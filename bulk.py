"""账号库的导入 / 导出 / 合并 / 脱敏 / 清理。

身份只认 uid：能从 JWT 解出 uid / 到期的就顺手补齐。两种格式都认：

* 平铺档（本项目的账号库、hub 的 accounts/）：uid 与 token 都在顶层；
* 客户端 .info 档（含 account 与 auth 两段，WB2api 的备份 / 导出）：凭证在 auth
  段，字段可能是 wbEncrypted 信封——导入时用本机 at-rest 密钥自动解开。

文本按形状识别：单个 JSON、JSON 数组、JSONL（一行一个凭证）、CSV、以及「一行一个
token」的纯文本。导出按后缀写：.json 合集包 / .jsonl 一行一个 .info / .info 单账号 /
.csv / .txt / .zip；剪贴板与 .jsonl 同构，可以粘回导入框或别的工具。

脱敏只改导出的那一份：token 留头尾、昵称与 uid 打码，账号文件本身不动。

编辑器登录态也在这里读：VS Code 系（state.vscdb 里 Chromium Safe Storage 的 v10 密文，
Windows 上用 DPAPI 解出 32 字节 key）与 JetBrains（secret-storage.xml 里的明文 JSON）。
只读，不写回，读出来的行同样交给 Bulk.merge。
"""
import base64
import csv
import io
import json
import os
import re
import time
import sqlite3
import sys
import zipfile
import xml.etree.ElementTree as ElementTree

from util import (REALMS, CoreError, as_dict, jwt_exp, jwt_uid, norm_ts, read_json,
                  say, write_json)

__all__ = ["Bulk", "points_to_token"]

PACK_VERSION = 1

ACCESS_KEYS = ("accessToken", "access_token", "accesstoken", "token", "access",
               "auth_token", "id_token")
REFRESH_KEYS = ("refreshToken", "refresh_token", "refreshtoken", "refresh", "rt")
UID_KEYS = ("uid", "userId", "user_id", "userid", "id", "uin")
NAME_KEYS = ("nickname", "nickName", "name", "username", "user_name", "displayName")
EXPIRE_KEYS = ("expiresAt", "expireAt", "expires_at", "expire_at", "expiredAt", "exp")
DOMAIN_KEYS = ("domain", "host", "server")
REALM_KEYS = ("realm", "region", "area", "zone")


def _first(row, keys):
    """按候选键名取第一个非空标量（大小写不敏感）。"""
    if not isinstance(row, dict):
        return ""
    lower = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            value = lower.get(str(key).lower())
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return value
    return ""


def _part(value):
    """JWT 的 payload；解不出来给空表。"""
    try:
        part = str(value or "").split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _envelope(value):
    """判断是不是 wbEncrypted 信封（字典，或它的 JSON 串）。"""
    node = value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            node = json.loads(value)
        except ValueError:
            return None
    return node if isinstance(node, dict) and "envelope" in node else None


def _text(value, key=None):
    """字段可能是明文、wbEncrypted 信封，或信封的 JSON 串；解不出来给空串。"""
    envelope = _envelope(value)
    if envelope is not None:
        if not key:
            return ""
        try:
            from vault import AtRest
            return AtRest.field(key, envelope) or ""
        except Exception:
            return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value or "")


class Bulk(object):
    """导入导出与账号库维护的总入口。"""

    # ---------- 规范化 ----------

    @staticmethod
    def flatten(row):
        """把客户端 .info（account / auth 两段）摊平成一条平铺档。"""
        if not isinstance(row, dict) or not any(item in row for item in ("account", "auth")):
            return row
        account = row.get("account") if isinstance(row.get("account"), dict) else {}
        auth = row.get("auth") if isinstance(row.get("auth"), dict) else {}
        out = {key: value for key, value in account.items()
               if key not in ("accounts", "allAccounts")}
        for src, dst in (("accessToken", "accessToken"), ("refreshToken", "refreshToken"),
                         ("domain", "domain"), ("expiresAt", "expiresAt"),
                         ("refreshExpiresAt", "refreshExpiresAt"),
                         ("tokenType", "tokenType"), ("scope", "scope")):
            if auth.get(src) not in (None, "") and out.get(dst) in (None, ""):
                out[dst] = auth.get(src)
        return out

    @classmethod
    def norm(cls, row, source="import", key=None):
        """把一条外来记录规范成账号 JSON；解不出 uid 就抛 CoreError。"""
        if not isinstance(row, dict):
            raise CoreError("记录不是 JSON 对象")
        row = cls.flatten(row)
        token = _text(_first(row, ACCESS_KEYS), key).strip()
        refresh = _text(_first(row, REFRESH_KEYS), key).strip()
        claims = _part(token)
        uid = (_text(_first(row, UID_KEYS), key).strip()
               or str(claims.get("sub") or "").strip())
        if not uid:
            raise CoreError("记录里解不出 uid（既没有 uid 字段，token 也不是 JWT）")
        out = dict(row)
        out["uid"] = uid
        if token:
            out["accessToken"] = token
        if refresh:
            out["refreshToken"] = refresh
        nickname = _text(_first(row, NAME_KEYS), key).strip()
        out["nickname"] = (nickname if nickname and nickname != "null"
                           else str(claims.get("name") or claims.get("nickname")
                                    or uid[:8]))
        realm = str(_first(row, REALM_KEYS) or "").strip().lower()
        if realm not in REALMS:
            domain = str(_text(_first(row, DOMAIN_KEYS), key) or "").lower()
            issuer = str(claims.get("iss") or "").lower()
            realm = ("cn" if ("codebuddy.cn" in domain or "tencent.com" in domain
                              or "codebuddy.cn" in issuer or "tencent.com" in issuer)
                     else "intl")
        out["realm"] = realm
        domain = _text(_first(row, DOMAIN_KEYS), key)
        out["domain"] = domain or REALMS[realm]["domain"]
        stamp = norm_ts(_first(row, EXPIRE_KEYS)) or int(claims.get("exp") or 0)
        if stamp:
            out["expiresAt"] = stamp
        out.setdefault("apiKey", False)
        out.setdefault("enabled", True)
        out.setdefault("tasksEnabled", True)
        out.setdefault("addedAt", time.time())
        out.setdefault("source", source)
        out.setdefault("platform", "CLI")
        out.setdefault("product", "workbuddy")
        if not str(out.get("enterpriseId") or ""):
            out.pop("enterpriseId", None)
        return out

    @staticmethod
    def missing(row):
        """这条记录还缺什么：没有 refreshToken 的账号不能续期，只能临时用。"""
        out = []
        if not str(row.get("accessToken") or ""):
            out.append("accessToken")
        if not str(row.get("refreshToken") or ""):
            out.append("refreshToken")
        if not str(row.get("expiresAt") or ""):
            out.append("expiresAt")
        return out

    # ---------- 解析 ----------

    @classmethod
    def parse(cls, text, key=None):
        """把一段文本解析成账号记录列表，返回 (rows, notes)。"""
        text = (text or "").strip().lstrip("\ufeff")
        if not text:
            raise CoreError("没有可导入的内容")
        notes = []
        table = cls._csv(text)
        if table is not None:
            return table, notes
        payload = None
        if text[0] in "{[":
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
        if payload is not None:
            rows = cls._walk(payload, key)
            if rows:
                return rows, notes
            notes.append("JSON 里没有找到账号记录，按文本行解析")
        rows = []
        for index, line in enumerate(text.splitlines(), 1):
            line = line.strip().lstrip("\ufeff")
            if not line or line.startswith("#"):
                continue
            if line[0] in "{[":
                try:
                    rows.extend(cls._walk(json.loads(line), key))
                except ValueError:
                    notes.append("第 %d 行：不是合法 JSON，跳过" % index)
                continue
            row, why = cls._line(line, key)
            if row:
                rows.append(row)
            elif why:
                notes.append("第 %d 行：%s" % (index, why))
        if not rows:
            raise CoreError("没有解析出任何账号（支持 JSON / JSONL / CSV / 每行一个 token）")
        return rows, notes

    @classmethod
    def _csv(cls, text):
        """带表头的 CSV（含 uid 与 token 列）按列名认字段；不是这种表就给 None。"""
        lines = [line for line in text.splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        if len(lines) < 2 or "," not in lines[0]:
            return None
        head = [cell.strip().strip('"').lower() for cell in lines[0].split(",")]
        if "uid" not in head and not any(cell in ("nickname", "name") for cell in head):
            return None
        if not any(cell in head for cell in ("accesstoken", "access_token", "token")):
            return None
        rows = []
        for row in csv.DictReader(io.StringIO("\n".join(lines))):
            try:
                rows.append(cls.norm(dict(row)))
            except CoreError:
                continue
        return rows or None

    @classmethod
    def _walk(cls, node, key=None):
        """在 JSON 结构里递归找账号：认 .info 两段、合集包外壳与数组。"""
        found = []
        if isinstance(node, list):
            for item in node:
                found.extend(cls._walk(item, key))
            return found
        if not isinstance(node, dict):
            return found
        if isinstance(node.get("account"), dict):
            try:
                found.append(cls.norm(node, key=key))
            except CoreError:
                pass
            if found:
                return found
        for name in ("accounts", "rows", "list", "items", "data", "result", "payload"):
            inner = node.get(name)
            if isinstance(inner, (list, dict)):
                found.extend(cls._walk(inner, key))
                if found:
                    return found
        if any(name in node for name in UID_KEYS + ACCESS_KEYS):
            try:
                found.append(cls.norm(node, key=key))
            except CoreError:
                pass
        return found

    @classmethod
    def _line(cls, line, key=None):
        """一行文本 → (账号, 失败原因)。分隔符认逗号 / 制表符 / 竖线 / 多连字符。"""
        parts = [line]
        for sep in (r"\s*,\s*", r"\t+", r"\s*\|\s*", r"\s*-{2,}\s*"):
            candidate = [part.strip().strip('"') for part in re.split(sep, line)]
            if len(candidate) >= 2:
                parts = candidate
                break
        if len(parts) == 1:
            token = parts[0]
            claims = _part(token)
            uid = str(claims.get("sub") or "")
            if not uid:
                return None, "既不是分隔符格式，也不是能解出 uid 的 token"
            realm = "cn" if "tencent" in str(claims.get("iss") or "").lower() else "intl"
            return {"uid": uid, "accessToken": token, "nickname": uid[:8], "realm": realm}, ""
        token_idx = [index for index, part in enumerate(parts) if points_to_token(part)]
        if not token_idx:
            return None, "这一行里没认出 token"
        access = parts[token_idx[0]]
        refresh = parts[token_idx[1]] if len(token_idx) > 1 else ""
        others = [part for index, part in enumerate(parts) if index not in token_idx]
        sub = str(_part(access).get("sub") or "")
        # uid 一律以 token 自己的 sub 为准（写错 uid 会让切换失败）；token 不是 JWT
        # 时才退回去认分隔符里那段像 id 的文字。
        uid = sub or (others[0] if others else "")
        nickname = next((part for part in others if part != uid), "")
        row = {"uid": uid, "accessToken": access, "refreshToken": refresh}
        if nickname:
            row["nickname"] = nickname
        if not row["uid"]:
            row["uid"] = str(_part(access).get("sub") or "")
        if not row["uid"]:
            return None, "解不出 uid"
        row.setdefault("nickname", row["uid"][:8])
        claims = _part(access)
        row["realm"] = ("cn" if "tencent" in str(claims.get("iss") or "").lower()
                        else "intl")
        return row, ""

    # ---------- 读文件 ----------

    @classmethod
    def read(cls, path, key=None):
        """按后缀读文件：.zip 解包逐个解析，其余按文本 parse。"""
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise CoreError("文件不存在：%s" % path)
        if path.lower().endswith(".zip"):
            return cls._read_zip(path, key)
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
        try:
            return cls.parse(text, key)
        except CoreError:
            if key:
                raise
            # 客户端导出的 .info 里 token 是 at-rest 信封：取一次本机密钥再试
            from vault import AtRest
            try:
                fallback = AtRest.recover()
            except Exception:
                raise
            return cls.parse(text, fallback)

    @classmethod
    def _read_zip(cls, path, key=None):
        """zip 里的每个 json / jsonl / info / csv / txt 都读一遍。"""
        rows, notes = [], []
        with zipfile.ZipFile(path) as bundle:
            for name in bundle.namelist():
                if not name.lower().endswith((".json", ".jsonl", ".info", ".csv", ".txt")):
                    continue
                try:
                    text = bundle.read(name).decode("utf-8-sig", "replace")
                    part, why = cls.parse(text, key)
                except Exception as exc:
                    notes.append("%s：%s" % (name, exc))
                    continue
                rows.extend(part)
                notes.extend("%s %s" % (name, item) for item in why)
        if not rows:
            raise CoreError("zip 里没有可导入的账号文件")
        return cls.dedupe(rows), notes

    @staticmethod
    def dedupe(rows):
        """同一批里同 uid 只留一份（凭证更全的优先，顺序按首次出现）。"""
        best, order = {}, []
        for row in rows:
            uid = str(row.get("uid") or "")
            score = (bool(row.get("accessToken")) + bool(row.get("refreshToken")),
                     norm_ts(row.get("expiresAt")))
            if uid not in best:
                order.append(uid)
                best[uid] = (score, row)
            elif score > best[uid][0]:
                best[uid] = (score, row)
        return [best[uid][1] for uid in order]

    # ---------- 合并 ----------

    @classmethod
    def merge(cls, store, rows, keep_newest=True, key=None):
        """把记录写进账号库：同 uid 的合并，更旧的凭证不覆盖已有的。"""
        added, updated, skipped, bad = [], [], [], []
        for raw in rows:
            try:
                row = cls.norm(raw, key=key)
            except CoreError as exc:
                bad.append(str(exc))
                continue
            old = store.by_uid.get(row["uid"])
            if old is not None and keep_newest:
                old_exp = norm_ts(old.data.get("expiresAt"))
                new_exp = norm_ts(row.get("expiresAt"))
                if row.get("accessToken") and old_exp > new_exp > 0:
                    skipped.append("%s 已在库中且凭证更旧，保留现有" % row["nickname"])
                    continue
            missing = cls.missing(row)
            if missing and old is None:
                bad.append("%s 缺少 %s，跳过" % (row.get("nickname"), "/".join(missing)))
                continue
            path = store.upsert(row)
            (updated if old is not None else added).append(
                "%s → %s" % (row.get("nickname") or row["uid"][:8], os.path.basename(path)))
        return {"added": added, "updated": updated, "skipped": skipped, "bad": bad,
                "count": len(added) + len(updated)}

    # ---------- 导出 ----------

    @staticmethod
    def mask(row):
        """脱敏副本：token 只留头尾，昵称与 uid 打码，其余字段原样。"""
        out = dict(row)
        for key in ("accessToken", "refreshToken"):
            if key not in out:
                continue
            value = str(out.get(key) or "")
            out[key] = ((value[:6] + "-" + value[-4:]) if len(value) > 16
                        else ("*" * 8 if value else ""))
        name = str(out.get("nickname") or "")
        if name:
            out["nickname"] = name[:1] + "*" * max(1, len(name) - 1)
        uid = str(out.get("uid") or "")
        if len(uid) > 8:
            out["uid"] = uid[:4] + "*" * (len(uid) - 8) + uid[-4:]
        out["masked"] = True
        return out

    @classmethod
    def pack(cls, rows, redact=False):
        """导出用的合集包（与 hub 的 workbuddy-accounts-*.json 同构）。"""
        items = [cls.mask(row) if redact else dict(row) for row in rows]
        return {"version": PACK_VERSION, "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "exportedBy": "WorkBuddy Switch", "masked": bool(redact),
                "count": len(items), "accounts": items}

    @staticmethod
    def info_doc(row):
        """一条账号 → 客户端 .info 形状（account / auth 两段，时间是毫秒）。"""
        stamp = norm_ts(row.get("expiresAt")) or int(jwt_exp(row.get("accessToken")))
        refresh_stamp = norm_ts(row.get("refreshExpiresAt"))
        account = {
            "uid": str(row.get("uid") or ""),
            "nickname": str(row.get("nickname") or ""),
            "uin": str(row.get("uin") or ""),
            "type": "personal",
            "lastLogin": True,
            "pluginEnabled": True,
            "accountType": str(row.get("accountType") or ""),
        }
        for name in ("phoneNumber", "phone", "enterpriseId", "enterpriseName"):
            if row.get(name):
                account["phoneNumber" if name == "phone" else name] = str(row[name])
        auth = {
            "accessToken": str(row.get("accessToken") or ""),
            "refreshToken": str(row.get("refreshToken") or ""),
            "tokenType": "Bearer",
            "domain": str(row.get("domain") or ""),
        }
        if row.get("sessionState"):
            auth["sessionState"] = str(row["sessionState"])
        if stamp:
            auth["expiresAt"] = stamp * 1000
            auth["expiresIn"] = max(0, stamp - int(time.time()))
        if refresh_stamp:
            auth["refreshExpiresAt"] = refresh_stamp * 1000
        if row.get("scope"):
            auth["scope"] = str(row["scope"])
        return {"account": account, "auth": auth}

    @classmethod
    def jsonl(cls, rows, redact=False):
        """导出成 JSONL：一行一个 .info 文档（与 WB2api 的导出文件同构）。"""
        lines = []
        for row in rows:
            item = cls.mask(row) if redact else row
            lines.append(json.dumps(cls.info_doc(item), ensure_ascii=False))
        return "\n".join(lines)

    @classmethod
    def clipboard(cls, rows, redact=False):
        """剪贴板内容 = 完整 JSONL，可直接粘回本工具的导入框或别的工具。"""
        return cls.jsonl(rows, redact)

    @classmethod
    def write(cls, path, rows, redact=False, fmt=""):
        """按后缀导出：.json 合集包 / .jsonl 或 .info 客户端档 / .csv / .txt / .zip。"""
        path = os.path.abspath(path)
        fmt = (fmt or os.path.splitext(path)[1].lstrip(".") or "json").lower()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        items = [cls.mask(row) if redact else dict(row) for row in rows]
        if fmt == "json":
            write_json(path, cls.pack(items))
        elif fmt == "jsonl":
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(cls.jsonl(items) + "\n")
        elif fmt == "info":
            if len(items) != 1:
                raise CoreError("单账号 .info 一次只能导出一个账号")
            write_json(path, cls.info_doc(items[0]))
        elif fmt == "csv":
            cls._write_csv(path, items)
        elif fmt in ("txt", "text"):
            cls._write_text(path, items)
        elif fmt == "zip":
            cls._write_zip(path, items)
        else:
            raise CoreError("不支持的导出格式：%s（可用 json / jsonl / info / csv / txt / zip）"
                            % fmt)
        return path, len(items)

    @staticmethod
    def _write_csv(path, rows):
        """CSV：带 BOM，列固定。"""
        cols = ("uid", "nickname", "realm", "domain", "accessToken", "refreshToken",
                "expiresAt", "expiresText", "enterpriseId")
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(cols)
            for row in rows:
                stamp = norm_ts(row.get("expiresAt"))
                writer.writerow([
                    row.get("uid", ""), row.get("nickname", ""), row.get("realm", ""),
                    row.get("domain", ""), row.get("accessToken", ""),
                    row.get("refreshToken", ""), row.get("expiresAt", ""),
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)) if stamp else "",
                    row.get("enterpriseId", "")])

    @staticmethod
    def _write_text(path, rows):
        """纯文本：一行一个账号，制表符分隔，能再导回来。"""
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# nickname\tuid\taccessToken\trefreshToken\n")
            for row in rows:
                fh.write("%s\t%s\t%s\t%s\n" % (row.get("nickname", ""), row.get("uid", ""),
                                               row.get("accessToken", ""),
                                               row.get("refreshToken", "")))

    @staticmethod
    def _write_zip(path, rows):
        """zip：每个账号一个 .info，再附一份合集包。"""
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
            for row in rows:
                name = re.sub(r'[\\/:*?"<>|]+', "_", str(row.get("nickname") or "account"))
                name = "%s-%s.info" % (name.strip(". ")[:40] or "account",
                                       str(row.get("uid") or "")[:8])
                bundle.writestr(name, json.dumps(Bulk.info_doc(row), ensure_ascii=False,
                                                 indent=2))
            bundle.writestr("accounts.json", json.dumps(
                Bulk.pack(rows), ensure_ascii=False, indent=2))

    # ---------- 从本机客户端目录导入 ----------

    @classmethod
    def scan_client(cls, log=None):
        """扫描客户端 auth 目录下所有 *.info，解出可导入的账号（不动当前登录态）。"""
        from client import Client
        rows, notes = [], []
        key, tried = None, False
        for base in Client.auth_dirs():
            if not os.path.isdir(base):
                continue
            for name in sorted(os.listdir(base)):
                if not name.endswith(".info") or ".bak" in name:
                    continue
                path = os.path.join(base, name)
                try:
                    doc = read_json(path)
                except Exception as exc:
                    notes.append("%s：读不了（%s）" % (name, exc))
                    continue
                if not isinstance(doc, dict) or not isinstance(doc.get("account"), dict):
                    notes.append("%s：不是 .info 结构" % name)
                    continue
                sealed = any(_envelope(value) is not None
                             for value in list((doc.get("auth") or {}).values())
                             + list((doc.get("account") or {}).values()))
                if sealed and not tried:
                    tried = True
                    try:
                        from vault import AtRest
                        key = AtRest.recover(log=log)
                    except Exception as exc:
                        notes.append("取 at-rest 密钥失败：%s" % exc)
                try:
                    row = cls.norm(doc, source="client", key=key)
                except CoreError as exc:
                    notes.append("%s：%s" % (name, exc))
                    continue
                row["source"] = "client"
                row["file"] = name
                rows.append(row)
        return cls.dedupe(rows), notes

    @classmethod
    def scan_ide(cls, log=None):
        """扫编辑器登录态（VS Code / CodeBuddy IDE / JetBrains），返回 (rows, notes)。"""
        return Editor.scan(log=log)


def points_to_token(value):
    """值看着像 JWT / 长 token（用于在分隔符行里分辨哪一段是凭证）。"""
    text = str(value or "").strip()
    if text.count(".") >= 2 and len(text) > 40:
        return True
    return len(text) >= 32 and bool(re.fullmatch(r"[A-Za-z0-9_\-\.=+/]+", text))


V10 = b"v10"
V11 = b"v11"
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


class Editor(object):
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
