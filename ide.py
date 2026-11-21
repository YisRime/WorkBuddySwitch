"""编辑器登录态导入：从 VS Code 扩展 / CodeBuddy IDE / JetBrains 插件读出已有登录。

这三个地方存的是同一份会话 JSON（id / token / refreshToken / expiresAt / domain /
account{uid,nickname,enterpriseId} / auth{accessToken,refreshToken,domain}），区别只在
怎么取出来：

* VS Code 与 CodeBuddy IDE：SQLite 库 state.vscdb 的 ItemTable，值是 Chromium Safe
  Storage 密文。Windows 上是 Local State 里被 DPAPI 保护过的 32 字节 key 加
  AES-256-GCM，密文形如 v10 || nonce(12) || ciphertext || tag(16)；值外层还包了一层
  {"type":"Buffer","data":[...]}。
* JetBrains：<配置根>/<产品>/options/secret-storage.xml 里的 Entry，值是明文 JSON。

只读：不写回、不要求客户端退出，读出来的行交给 Bulk.merge 进账号库。Windows 之外的
Safe Storage 走的是 AES-128-CBC，本工具只实现 Windows 这条路，其它平台会如实报错。
"""
import base64
import json
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ElementTree

from bulk import Bulk
from util import CoreError, as_dict, norm_ts, say

V10 = b"v10"
V11 = b"v11"
SALT = b"saltysalt"
DPAPI_TAG = b"DPAPI"


def _appdata():
    """Windows 的 %APPDATA%；其它平台返回空串。"""
    if os.name != "nt":
        return ""
    return os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData",
                                                    "Roaming")


def _jetbrains_root():
    """JetBrains 的配置根目录（每个产品一个子目录）。"""
    if os.name == "nt":
        return os.path.join(_appdata(), "JetBrains")
    home = os.path.expanduser("~")
    return (os.path.join(home, "Library", "Application Support", "JetBrains")
            if sys.platform == "darwin" else os.path.join(home, ".config", "JetBrains"))


class Ide(object):
    """编辑器 / 插件的登录态：定位、解密、摊平成账号库的一行。"""

    #: 每类目标：显示名、%APPDATA% 下的目录名、库文件候选（相对目录）、是否 XML。
    TARGETS = (
        {"name": "VS Code 扩展", "folders": ("Code", "Code - Insiders"),
         "kind": "sqlite"},
        {"name": "CodeBuddy CN IDE", "folders": ("CodeBuddy CN",), "kind": "sqlite"},
        {"name": "CodeBuddy IDE", "folders": ("CodeBuddy",), "kind": "sqlite"},
    )
    DB_TAILS = (os.path.join("User", "globalStorage", "state.vscdb"),
                os.path.join("globalStorage", "state.vscdb"),
                "state.vscdb")
    #: 认得出的 secret 键（VS Code 扩展 / CN IDE / 国际版 IDE）。
    SECRET_HINTS = ("Tencent-Cloud.coding-copilot", "planning-genie.new.accessToken")

    # ---------- 定位 ----------

    @classmethod
    def db_paths(cls):
        """本机存在的 state.vscdb：[(目标名, 数据根, 库路径)]。"""
        found, seen = [], set()
        for target in cls.TARGETS:
            for folder in target["folders"]:
                root = os.path.join(_appdata(), folder)
                if not os.path.isdir(root):
                    continue
                for tail in cls.DB_TAILS:
                    path = os.path.join(root, tail)
                    if os.path.isfile(path) and path not in seen:
                        seen.add(path)
                        found.append((target["name"], root, path))
                        break
        return found

    @classmethod
    def jetbrains_files(cls):
        """本机存在的 JetBrains secret-storage.xml 列表。"""
        root = _jetbrains_root()
        found = []
        if not os.path.isdir(root):
            return found
        for product in sorted(os.listdir(root)):
            path = os.path.join(root, product, "options", "secret-storage.xml")
            if os.path.isfile(path):
                found.append(path)
        return found

    # ---------- Safe Storage 解密 ----------

    @staticmethod
    def decode_value(text):
        """ItemTable 的值 → 原始字节：可能是 {"type":"Buffer","data":[...]}，也可能就是串。"""
        if text is None:
            return b""
        if isinstance(text, bytes):
            return text
        value = str(text)
        try:
            doc = json.loads(value)
        except ValueError:
            return value.encode("utf-8")
        if isinstance(doc, dict) and doc.get("type") == "Buffer" and isinstance(
                doc.get("data"), list):
            return bytes(byte & 0xFF for byte in doc["data"])
        if isinstance(doc, str):
            return doc.encode("utf-8")
        return value.encode("utf-8")

    @staticmethod
    def dpapi(blob):
        """调用 Windows DPAPI 解开一段密文（当前用户作用域）。"""
        if os.name != "nt":
            raise CoreError("DPAPI 只在 Windows 上可用")
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            """DATA_BLOB，两个字段：长度与指针。"""

            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buffer = ctypes.create_string_buffer(blob, len(blob))
        source = Blob(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        target = Blob()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target))
        if not ok:
            raise CoreError("DPAPI 解密失败（错误码 %s）" % ctypes.get_last_error())
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            kernel32.LocalFree(target.pbData)

    @classmethod
    def local_key(cls, root):
        """从 <数据根>/Local State 取 32 字节 Safe Storage key。"""
        path = os.path.join(root, "Local State")
        if not os.path.isfile(path):
            raise CoreError("没有 Local State，说明这个客户端还没登录过：%s" % root)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception as exc:
            raise CoreError("读不了 Local State：%s" % exc)
        encoded = str(as_dict(as_dict(doc.get("os_crypt")).get("encrypted_key")) or "")
        if not encoded:
            raise CoreError("Local State 里没有 os_crypt.encrypted_key")
        try:
            blob = base64.b64decode(encoded)
        except Exception:
            raise CoreError("encrypted_key 不是合法 base64")
        if not blob.startswith(DPAPI_TAG):
            raise CoreError("encrypted_key 缺少 DPAPI 前缀（不是 Windows 的 Chrome 形态）")
        key = cls.dpapi(blob[len(DPAPI_TAG):])
        if len(key) != 32:
            raise CoreError("Safe Storage key 长度是 %d，预期 32" % len(key))
        return key

    @staticmethod
    def decrypt(blob, key):
        """解一段 Safe Storage 密文（只支持 Windows 的 v10 + AES-256-GCM）。"""
        if not blob:
            raise CoreError("密文是空的")
        if blob.startswith(V11) or (blob.startswith(V10) and os.name != "nt"):
            raise CoreError("这个平台用的是 AES-128-CBC 的 v10/v11，暂不支持")
        if not blob.startswith(V10):
            # 历史明文：没见过前缀就当明文
            try:
                return blob.decode("utf-8").encode("utf-8")
            except Exception:
                raise CoreError("不认识的密文格式（开头不是 v10）")
        body = blob[len(V10):]
        if len(body) < 12 + 16:
            raise CoreError("密文太短（%d 字节），不像 v10 的 nonce + 密文 + tag" % len(blob))
        from vault import gcm_open
        nonce, rest = body[:12], body[12:]
        return gcm_open(key, nonce, b"", rest[:-16], rest[-16:])

    # ---------- 读库 ----------

    @classmethod
    def _rows(cls, path):
        """只读打开 state.vscdb，把 ItemTable 里带 secret 键的行拿出来。"""
        if not os.path.isfile(path):
            return []
        uri = "file:%s?mode=ro" % path.replace("?", "%3f").replace("#", "%23")
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5)
        except sqlite3.Error as exc:
            raise CoreError("打不开 %s：%s" % (os.path.basename(path), exc))
        try:
            try:
                records = conn.execute("SELECT key, value FROM ItemTable").fetchall()
            except sqlite3.Error:
                return []
        finally:
            conn.close()
        out = []
        for key, value in records:
            text = str(key or "")
            if cls.SECRET_HINTS and not any(hint in text for hint in cls.SECRET_HINTS):
                continue
            out.append((text, value))
        return out

    @staticmethod
    def session_row(payload, target=""):
        """一份会话 JSON → 账号库的一行（uid 取 account.uid / account.id / token 前缀）。"""
        payload = as_dict(payload)
        account = as_dict(payload.get("account"))
        auth = as_dict(payload.get("auth"))
        access = str(auth.get("accessToken") or payload.get("token") or "")
        refresh = str(auth.get("refreshToken") or payload.get("refreshToken") or "")
        uid = str(account.get("uid") or account.get("id") or "")
        if not uid:
            shaped = str(payload.get("accessToken") or "")
            if "+" in shaped:
                uid = shaped.rsplit("+", 1)[0]
        if not uid and access:
            from util import jwt_uid
            uid = jwt_uid(access)
        if not uid:
            raise CoreError("会话里没有 uid")
        stamp = norm_ts(auth.get("expiresAt") or payload.get("expiresAt"))
        row = {
            "uid": uid,
            "nickname": str(account.get("nickname") or account.get("label") or uid[:8]),
            "accessToken": access,
            "refreshToken": refresh,
            "domain": str(auth.get("domain") or payload.get("domain") or ""),
            "expiresAt": stamp,
            "enterpriseId": str(account.get("enterpriseId") or ""),
            "source": "ide",
            "platform": target or "IDE",
            "product": "workbuddy",
        }
        return Bulk.norm(row, source="ide")

    @classmethod
    def scan(cls, log=None):
        """扫描本机所有编辑器登录态，返回 (rows, notes)。"""
        rows, notes = [], []
        for name, root, path in cls.db_paths():
            try:
                records = cls._rows(path)
            except CoreError as exc:
                notes.append("%s：%s" % (name, exc))
                continue
            if not records:
                notes.append("%s：库里还没有登录行（先在编辑器里登录一次）" % name)
                continue
            try:
                key = cls.local_key(root)
            except CoreError as exc:
                notes.append("%s：%s" % (name, exc))
                continue
            for index, (_key, value) in enumerate(records, 1):
                try:
                    plain = cls.decrypt(cls.decode_value(value), key)
                    payload = json.loads(plain.decode("utf-8", "replace"))
                except Exception as exc:
                    notes.append("%s 第 %d 条解密失败：%s" % (name, index, exc))
                    continue
                try:
                    rows.append(cls.session_row(payload, name))
                except CoreError as exc:
                    notes.append("%s 第 %d 条解析失败：%s" % (name, index, exc))
            say(log, "%s：读到 %d 条登录态" % (name, len(records)))
        for path in cls.jetbrains_files():
            try:
                pairs = cls.jetbrains_entries(path)
            except Exception as exc:
                notes.append("JetBrains %s：%s" % (os.path.basename(os.path.dirname(
                    os.path.dirname(path))), exc))
                continue
            for entry_key, value in pairs:
                if not any(hint in entry_key for hint in cls.SECRET_HINTS):
                    continue
                try:
                    payload = json.loads(value)
                    rows.append(cls.session_row(payload, "JetBrains"))
                except Exception as exc:
                    notes.append("JetBrains 条目解析失败：%s" % exc)
        return rows, notes

    @staticmethod
    def jetbrains_entries(path):
        """读 secret-storage.xml，返回 [(key, 明文 value)]。"""
        try:
            tree = ElementTree.parse(path)
        except Exception as exc:
            raise CoreError("读不了 XML：%s" % exc)
        pairs = []
        for node in tree.iter():
            if node.tag.split("}")[-1] != "Entry":
                continue
            key = node.get("key") or ""
            value = node.get("value") or ""
            if key and value:
                pairs.append((key, value))
        return pairs
