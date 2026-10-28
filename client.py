"""客户端登录态：auth 文件、token 刷新、会话构造、切换 / 同步与快照恢复。

auth 文件是 JSON，account / auth 两个槽位里的 token 与昵称是 at-rest 信封；切换时
整份重写，独立 CLI 槽位用明文。所有写盘动作都先落一份 Snapshot，失败自动回滚。
"""
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

import vault
from slot import Slot
from util import (CLI_INFO, REALMS, CoreError, Process, as_dict, jwt_claims, jwt_exp,
                  jwt_uid, norm_ts, read_json, realm_label, realm_of, say, url_open,
                  write_json)


class Client:
    """客户端登录态：定位 auth 文件、刷新 token、构造会话。"""

    @classmethod
    def auth_dirs(cls):
        """可能的 auth 目录：Windows 一处，macOS / Linux 有多处候选。"""
        if os.name == "nt":
            local = os.environ.get("LOCALAPPDATA") or os.path.join(
                os.path.expanduser("~"), "AppData", "Local")
            return [os.path.join(local, "CodeBuddyExtension", "Data", "Public", "auth")]
        home = os.path.expanduser("~")
        base = (os.path.join(home, "Library", "Application Support") if sys.platform == "darwin"
                else os.path.join(home, ".local", "share"))
        return [os.path.join(base, app, "Data", "Public", "auth")
                for app in ("CodeBuddyExtension", "WorkBuddy", "CodeBuddy")]

    @classmethod
    def desktop_auth(cls):
        """返回 (auth 文件路径, realm)：取最近修改且未登出的那份。"""
        candidates = []
        for base in cls.auth_dirs():
            if not os.path.isdir(base):
                continue
            for realm, cfg in REALMS.items():
                path = os.path.join(base, cfg["info"])
                if os.path.isfile(path) and not os.path.exists(path + ".logged-out"):
                    candidates.append((path, realm))
        if not candidates:
            raise CoreError("没有找到客户端登录态（先在客户端登录一次）")
        return max(candidates, key=lambda item: os.path.getmtime(item[0]))

    @classmethod
    def cli_auth(cls):
        """独立 CLI 槽位：已有文件就返回它，还没有就给首选目录里的目标路径。"""
        fallback = ""
        for base in cls.auth_dirs():
            path = os.path.join(base, CLI_INFO)
            if os.path.isfile(path):
                return path
            fallback = fallback or path
        return fallback

    @classmethod
    def cli_current(cls):
        """独立 CLI 槽位当前登录的摘要（明文）；没登录过或已登出为空。"""
        path = cls.cli_auth()
        if not path or os.path.exists(path + ".logged-out"):
            return {"uid": "", "nickname": ""}
        try:
            account = read_json(path).get("account") or {}
        except Exception:
            account = {}
        return {"uid": str(account.get("uid") or ""),
                "nickname": str(account.get("nickname") or "")}

    @classmethod
    def db_path(cls):
        """客户端数据库 workbuddy.db 的路径。"""
        return os.path.join(vault.AtRest.config_dir(), "workbuddy.db")

    @classmethod
    def current(cls):
        """当前 GUI 登录态摘要；不扫进程表，免得启动被 tasklist 拖住。"""
        uid, realm = "", ""
        try:
            path, realm = cls.desktop_auth()
            uid = str((read_json(path).get("account") or {}).get("uid") or "")
        except Exception:
            pass
        return {"uid": uid, "realm": realm}

    @staticmethod
    def http_json(url, data=None, method=None, headers=None, timeout=25, proxy="",
                  retries=2) -> dict:
        """发一个请求并解析 JSON；400 / 401 / 403 不重试，其余失败退避重试。"""
        last = None
        for attempt in range(max(1, retries)):
            try:
                req = urllib.request.Request(
                    url, data=data, method=method or ("POST" if data is not None else "GET"),
                    headers=headers or {})
                with url_open(req, proxy, timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise CoreError("响应不是 JSON 对象")
                return payload
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                last = CoreError("HTTP %s %s" % (exc.code, body))
                if exc.code in (400, 401, 403):
                    break
            except Exception as exc:
                last = exc
            if attempt + 1 < retries:
                time.sleep(0.8 * (attempt + 1))
        raise CoreError("请求失败：%s" % last)

    @classmethod
    def profiles(cls, account):
        """上游 /v2/accounts：切换登录态时要写进 auth 的档案列表。"""
        cfg = REALMS[realm_of(account)]
        payload = cls.http_json(
            cfg["chat"] + "/v2/accounts", method="GET",
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": cfg["ua"],
                "Origin": cfg["origin"],
                "Referer": cfg["origin"] + "/",
                "Authorization": "Bearer " + str(account.get("accessToken") or ""),
                "X-Domain": str(account.get("domain") or cfg["domain"]),
            }, proxy=str(account.get("proxy") or ""))
        rows = [row for row in (as_dict(payload.get("data")).get("accounts") or [])
                if isinstance(row, dict) and row.get("uid")]
        if not any(str(row.get("uid")) == str(account.get("uid") or "") for row in rows):
            raise CoreError("上游 /v2/accounts 没有目标账号（token 与 uid 不匹配？）")
        return rows

    @classmethod
    def refresh(cls, account, log=None):
        """用 refreshToken 换新 token，并把新凭证写回账号字典。"""
        if bool(account.get("apiKey")):
            raise CoreError("静态 API Key 没有 refreshToken")
        cfg = REALMS[realm_of(account)]
        token = str(account.get("refreshToken") or "")
        if not token:
            raise CoreError("账号没有 refreshToken")
        realm, uid = realm_of(account), str(account.get("uid") or "")
        domain = str(account.get("domain") or cfg["domain"])
        if realm == "cn":
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": cfg["billing_ua"],
                "Origin": cfg["origin"],
                "Referer": cfg["origin"] + "/",
                "X-Refresh-Token": token,
                "X-Auth-Refresh-Source": "workbuddy",
                "X-User-Id": uid,
                "X-Domain": domain,
                "X-CodeBuddy-Request": "1",
                "Accept-Language": "zh-CN",
            }
        else:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "X-Domain": domain,
                "X-Refresh-Token": token,
                "X-Auth-Refresh-Source": "plugin",
                "X-Request-ID": uuid.uuid4().hex,
                "X-Product": "SaaS",
                "User-Agent": cfg["ua"],
                "Authorization": "Bearer " + str(account.get("accessToken") or ""),
                "X-User-Id": uid,
                "Accept-Encoding": "gzip, deflate",
            }
        if account.get("enterpriseId"):
            headers["X-Enterprise-Id"] = str(account["enterpriseId"])
        payload = cls.http_json(cfg["chat"] + "/v2/plugin/auth/token/refresh", data=b"{}",
                                method="POST", headers=headers,
                                proxy=str(account.get("proxy") or ""))
        data = as_dict(payload.get("data"))
        data = as_dict(data.get("data")) or data
        new_token = data.get("accessToken")
        if not new_token:
            raise CoreError("刷新返回没有 accessToken：%s" % (payload.get("msg") or payload.get("code")))
        account["accessToken"] = new_token
        account["refreshToken"] = data.get("refreshToken") or token
        account["expiresAt"] = jwt_exp(new_token) or account.get("expiresAt")
        say(log, "凭证已刷新：%s" % uid[:8])
        return account

    @classmethod
    def _prepare(cls, account, profiles):
        """两种槽位共用的前置：校验凭证、定位目标档案、算到期时间。"""
        uid = str(account.get("uid") or "")
        token = str(account.get("accessToken") or "")
        refresh = str(account.get("refreshToken") or "")
        if not uid or not token or not refresh:
            raise CoreError("账号缺少 uid / accessToken / refreshToken")
        if jwt_uid(token) and jwt_uid(token) != uid:
            raise CoreError("accessToken 的 sub 与 uid 不一致")
        target = next((row for row in profiles if str(row.get("uid")) == uid), None)
        if target is None:
            raise CoreError("上游档案里没有目标账号")
        exp = norm_ts(account.get("expiresAt")) or jwt_exp(token)
        return uid, token, refresh, target, exp, jwt_exp(refresh) or exp

    @staticmethod
    def _timing(now, exp, refresh_exp):
        """auth 里两种槽位共用的时间字段。"""
        return {"expiresIn": max(0, exp - now), "refreshExpiresIn": max(0, refresh_exp - now),
                "lastRefreshTime": now * 1000, "expiresAt": exp * 1000,
                "refreshExpiresAt": refresh_exp * 1000}

    @staticmethod
    def merge_profile(template, profile, target_uid):
        """把上游档案并进 auth 的 account 槽位，补齐前端要的默认字段。"""
        base = dict(template or {})
        for key in ("uid", "uin", "type", "lastLogin", "pluginEnabled", "deployStatus",
                    "accountType", "sso", "oneidAccountId", "isCurrentOneIdEnterprise",
                    "isCurrentOneIdPersonal"):
            if key in profile:
                base[key] = profile[key]
        base["uid"] = str(profile.get("uid") or "")
        base["lastLogin"] = base["uid"] == str(target_uid)
        for key, value in (("type", "personal"), ("pluginEnabled", True), ("isCreator", False),
                           ("isAdmin", False), ("isFirstLogin", False), ("logOutEnable", False),
                           ("isCurrentOneIdPersonal", False)):
            base.setdefault(key, value)
        base["areaInfoComplete"] = True
        base.pop("phoneNumber", None)
        return base

    @classmethod
    def build_session(cls, account, template, key, profiles):
        """把一条账号 + 上游档案 + at-rest 密钥拼成 auth/*.info 的内容。"""
        uid, token, refresh, target, exp, refresh_exp = cls._prepare(account, profiles)
        cfg = REALMS[realm_of(account)]
        base = cls.merge_profile(template.get("account") or {}, target, uid)
        base["nickname"] = vault.AtRest.seal(
            key, str(target.get("nickname") or account.get("nickname") or uid[:8]), "field")
        auth = dict(template.get("auth") or {})
        auth.update(cls._timing(int(time.time()), exp, refresh_exp))
        auth.update({
            "accessToken": vault.AtRest.seal(key, token, "field"),
            "refreshToken": vault.AtRest.seal(key, refresh, "field"),
            "tokenType": "Bearer",
            "sessionState": str(uuid.uuid4()),
            "scope": str(jwt_claims(token).get("scope") or auth.get("scope")
                         or "openid profile offline_access email"),
            "domain": str(account.get("domain") or cfg["domain"]),
        })
        accounts = []
        for row in profiles:
            merged = cls.merge_profile(template.get("account") or {}, row, uid)
            merged["nickname"] = vault.AtRest.seal(
                key, str(row.get("nickname") or merged.get("uid") or ""), "field")
            accounts.append(merged)
        return {"account": base, "auth": auth, "accounts": accounts,
                "allAccounts": [dict(row) for row in accounts]}

    @classmethod
    def build_cli_session(cls, account, profiles):
        """独立 CLI 槽位的内容：明文 token、无 allAccounts、domain 用 cli_domain。"""
        uid, token, refresh, target, exp, refresh_exp = cls._prepare(account, profiles)
        cfg = REALMS[realm_of(account)]
        plain = {key: value for key, value in (target or {}).items()
                 if not isinstance(value, (dict, list))}
        plain.update(uid=str(uid), lastLogin=True, pluginEnabled=True, type="personal")
        auth = cls._timing(int(time.time()), exp, refresh_exp)
        auth.update({
            "accessToken": token,
            "refreshToken": refresh,
            "tokenType": "Bearer",
            "sessionState": str(uuid.uuid4()),
            "scope": str(jwt_claims(token).get("scope")
                         or "openid profile offline_access email"),
            "domain": cfg.get("cli_domain") or cfg["domain"],
        })
        return {"account": plain, "auth": auth, "accounts": [dict(plain)]}

    @classmethod
    def session_summary(cls, doc, key, sealed=True):
        """写入后回读：uid 与 token 都能解出来，且 sub 对得上。"""
        account, auth = doc.get("account") or {}, doc.get("auth") or {}
        uid = str(account.get("uid") or "")
        if sealed:
            access = vault.AtRest.field(key, auth.get("accessToken"))
            refresh = vault.AtRest.field(key, auth.get("refreshToken"))
            nickname = vault.AtRest.field(key, account.get("nickname")) or ""
        else:
            access = str(auth.get("accessToken") or "")
            refresh = str(auth.get("refreshToken") or "")
            nickname = str(account.get("nickname") or "")
        if not uid or not access or not refresh:
            raise CoreError("写入后回读失败：uid / token 解不出来")
        if jwt_uid(access) != uid:
            raise CoreError("写入后回读失败：token sub 与 uid 不一致")
        return {"uid": uid,
                "nickname": nickname,
                "uin": str(account.get("uin") or ""),
                "domain": str(auth.get("domain") or ""),
                "expiresAt": int(auth.get("expiresAt") or 0)}

    @classmethod
    def capture(cls, log=None):
        """把当前客户端登录态读成一条账号记录（收录用，字段与 hub 凭证档同构）。"""
        path, realm = cls.desktop_auth()
        key = vault.AtRest.recover(log=log)
        doc = read_json(path)
        account, auth = doc.get("account") or {}, doc.get("auth") or {}
        uid = str(account.get("uid") or "")
        access = vault.AtRest.field(key, auth.get("accessToken")) or ""
        refresh = vault.AtRest.field(key, auth.get("refreshToken")) or ""
        if not (uid and access and refresh):
            raise CoreError("当前登录态不完整（缺 uid / token）")
        return {
            "uid": uid,
            "nickname": vault.AtRest.field(key, account.get("nickname")) or uid[:8],
            "domain": str(auth.get("domain") or REALMS[realm]["domain"]),
            "realm": realm,
            "platform": "CLI",
            "product": "workbuddy",
            "enterpriseId": str(account.get("enterpriseId") or ""),
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": norm_ts(auth.get("expiresAt")) or jwt_exp(access),
            "addedAt": time.time(),
            "source": "capture",
            "apiKey": False,
            "enabled": True,
            "tasksEnabled": True,
        }

    @classmethod
    def sync_sessions(cls, uid, log=None):
        """把本机所有有 JSONL 的本地会话挂到 uid 名下（云端会话不动）。"""
        uid = str(uid or "").strip()
        if not uid:
            raise CoreError("缺少目标 uid")
        db_path = cls.db_path()
        if not os.path.isfile(db_path):
            return {"ok": True, "skipped": "no-db", "moved": 0, "eligible": 0}
        local_ids = set()
        for _dirpath, _dirs, files in os.walk(os.path.join(os.path.dirname(db_path), "projects")):
            local_ids.update(name[:-6] for name in files if name.endswith(".jsonl"))
        conn = sqlite3.connect(db_path, timeout=20)
        try:
            rows = conn.execute(
                "SELECT id, user_id FROM sessions WHERE transport = 'local' AND deleted_at IS NULL"
            ).fetchall()
            eligible = [(str(sid), str(owner or "")) for sid, owner in rows if str(sid) in local_ids]
            already = sum(1 for _sid, owner in eligible if owner == uid)
            cloud = conn.execute("SELECT count(*) FROM sessions WHERE transport = 'cloud'").fetchone()[0]
            if eligible and already == len(eligible):
                return {"ok": True, "moved": 0, "eligible": len(eligible), "already": already,
                        "cloudSkipped": int(cloud or 0), "skipped": "already"}
            columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
            if "wb_original_user_id" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN wb_original_user_id TEXT")
            for sid, owner in eligible:
                conn.execute(
                    "UPDATE sessions SET wb_original_user_id = COALESCE(wb_original_user_id, ?), "
                    "user_id = ? WHERE id = ?", (owner, uid, sid))
            conn.commit()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass
            say(log, "本地会话已合并到 %s：%d 条（云端跳过 %s）" % (uid[:8], len(eligible), cloud))
            return {"ok": True, "moved": len(eligible), "eligible": len(eligible),
                    "already": already, "cloudSkipped": int(cloud or 0)}
        except sqlite3.DatabaseError as exc:
            raise CoreError("同步本地会话失败：%s" % exc)
        finally:
            conn.close()


class Snapshot:
    """备份 / 恢复：auth 文件、CLI 登录态与 workbuddy.db 的整组快照。"""

    AUTH = "auth.info"
    DB = "workbuddy.db"
    CLI_AUTH = "cli-auth.info"

    @staticmethod
    def clear(path):
        """清空目录（不删目录本身），返回路径。"""
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            return path
        for name in os.listdir(path):
            item = os.path.join(path, name)
            if os.path.isdir(item) and not os.path.islink(item):
                shutil.rmtree(item)
            else:
                try:
                    os.unlink(item)
                except OSError:
                    pass
        return path

    @classmethod
    def save(cls, dest_dir, log=None):
        """把当前登录态与数据库复制进备份目录，并写 manifest。"""
        auth_path, realm = Client.desktop_auth()
        db_path = Client.db_path()
        dest, files = cls.clear(dest_dir), []

        def copy(src, name):
            """复制一个文件进备份目录，并把大小与 sha256 记进清单。"""
            if not os.path.isfile(src):
                return
            with open(src, "rb") as fh:
                blob = fh.read()
            shutil.copy2(src, os.path.join(dest, name))
            files.append({"name": name, "bytes": len(blob),
                          "sha256": hashlib.sha256(blob).hexdigest()})

        copy(auth_path, cls.AUTH)
        copy(auth_path + ".logged-out", cls.AUTH + ".logged-out")
        copy(Client.cli_auth(), cls.CLI_AUTH)
        for suffix in ("", "-wal", "-shm"):
            copy(db_path + suffix, cls.DB + suffix)
        write_json(os.path.join(dest, "manifest.json"), {
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "authSource": auth_path, "dbSource": db_path, "realm": realm, "files": files,
        })
        say(log, "备份已更新：%s（%d 个文件）" % (dest, len(files)))
        return {"ok": True, "path": dest, "files": [item["name"] for item in files]}

    @classmethod
    def restore(cls, dest_dir, restart=True, log=None):
        """把备份写回原位；需要时先停客户端，恢复后再拉起来。"""
        dest = os.path.abspath(dest_dir)
        manifest_path = os.path.join(dest, "manifest.json")
        if not os.path.isfile(manifest_path):
            raise CoreError("没有备份：%s" % dest)
        manifest = as_dict(read_json(manifest_path))
        auth_path = str(manifest.get("authSource") or "")
        try:
            current, _realm = Client.desktop_auth()
            if os.path.basename(current) == os.path.basename(auth_path):
                auth_path = current
        except Exception:
            if not auth_path:
                auth_path, _realm = Client.desktop_auth()
        db_path = str(manifest.get("dbSource") or "")
        current_db = Client.db_path()
        if not db_path or os.path.basename(db_path) == os.path.basename(current_db):
            db_path = current_db
        if not os.path.isfile(os.path.join(dest, cls.AUTH)):
            raise CoreError("备份缺少 auth.info")
        cli_backup = os.path.join(dest, cls.CLI_AUTH)
        cli_slot = Client.cli_auth() if os.path.isfile(cli_backup) else ""
        running = bool(Process.pids())
        if restart and running:
            Process.stop(log=log)
        if restart and cli_slot and Process.cli_pids():
            Process.stop(cli=True, log=log, keep=Slot().claimed_pids())
        restored = []
        try:
            marker = auth_path + ".logged-out"
            if os.path.exists(marker):
                os.unlink(marker)
            shutil.copy2(os.path.join(dest, cls.AUTH), auth_path)
            restored.append(cls.AUTH)
            backup_marker = os.path.join(dest, cls.AUTH + ".logged-out")
            if os.path.isfile(backup_marker):
                shutil.copy2(backup_marker, marker)
            if cli_slot:
                os.makedirs(os.path.dirname(cli_slot) or ".", exist_ok=True)
                shutil.copy2(cli_backup, cli_slot)
                restored.append(cls.CLI_AUTH)
            for suffix in ("-wal", "-shm"):
                if os.path.exists(db_path + suffix):
                    try:
                        os.unlink(db_path + suffix)
                    except OSError:
                        pass
            if os.path.isfile(os.path.join(dest, cls.DB)):
                shutil.copy2(os.path.join(dest, cls.DB), db_path)
                restored.append(cls.DB)
                for suffix in ("-wal", "-shm"):
                    if os.path.isfile(os.path.join(dest, cls.DB + suffix)):
                        shutil.copy2(os.path.join(dest, cls.DB + suffix), db_path + suffix)
            started = Process.start(log=log) if restart else False
            say(log, "备份已恢复")
            return {"ok": True, "started": started, "restored": restored,
                    "authPath": auth_path, "dbPath": db_path}
        except Exception:
            if restart:
                Snapshot.resume(running, log)
            raise

    @classmethod
    @contextlib.contextmanager
    def guarded(cls, dest_dir, log=None):
        """先备份，块内失败自动回滚。"""
        cls.save(dest_dir, log=log)
        try:
            yield
        except Exception:
            try:
                cls.restore(dest_dir, restart=False, log=log)
            except Exception:
                pass
            raise

    @staticmethod
    def resume(running, log):
        """异常回滚：原本在运行、现在却没进程了，就试着把客户端拉回来。"""
        if running and not Process.pids():
            try:
                Process.start(log=log)
            except Exception:
                pass


class Switcher:
    """切换 / 同步 / 恢复的总入口：全局串行，写盘前先停客户端。"""

    _lock = threading.Lock()

    @classmethod
    def _guard(cls):
        """拿全局切换锁；拿不到说明已有同类操作在跑。"""
        if not cls._lock.acquire(False):
            raise CoreError("已有切换 / 同步 / 恢复操作在进行")

    @classmethod
    @contextlib.contextmanager
    def _stopped(cls, log):
        """块内停掉客户端，yield 原先是否在运行；异常退出且没拉起来时试着重启。"""
        running = bool(Process.pids())
        if running:
            Process.stop(log=log)
        try:
            yield running
        except Exception:
            Snapshot.resume(running, log)
            raise

    @classmethod
    def switch(cls, account, backup_dir, sync=False, log=None):
        """切换桌面端登录态：跨区拒绝，写盘后回读校验并重启客户端。"""
        cls._guard()
        try:
            path, realm = Client.desktop_auth()
            if realm_of(account) != realm:
                raise CoreError("目标账号是%s，客户端当前是%s；跨区不能切换"
                                % (realm_label(realm_of(account)), realm_label(realm)))
            key = vault.AtRest.recover(log=log)
            doc = Client.build_session(account, read_json(path), key, Client.profiles(account))
            with cls._stopped(log):
                with Snapshot.guarded(backup_dir, log=log):
                    write_json(path, doc)
                    info = Client.session_summary(read_json(path), key)
                    if info["uid"] != str(account.get("uid")):
                        raise CoreError("写入后回读 uid 不一致")
                    sessions = Client.sync_sessions(account.get("uid"), log=log) if sync else {}
                    started = Process.start(log=log)
                    applied = Process.wait_uid(path, str(account.get("uid"))) if started else False
                say(log, "已切换到 %s（%s）" % (info["nickname"], str(info["uid"])[:8]))
                return {"uid": info["uid"], "nickname": info["nickname"], "started": started,
                        "applied": applied, "moved": (sessions or {}).get("moved", 0),
                        "sessions": sessions}
        finally:
            cls._lock.release()

    @staticmethod
    def _same_uid_warning(slots, account, exclude, log):
        """同一账号已挂在别的实例时给风控提示（不阻止）。"""
        same = [m for m in slots.list()
                if str(m.get("uid") or "") == str(account.get("uid") or "")
                and m["name"] != exclude]
        if same:
            say(log, "注意：账号 %s 已挂在实例 %s；同一账号多处登录有被上游轮换 "
                     "refreshToken 挤下线的风险" % (account.get("nickname")
                                                    or account.get("uid"),
                                                    "、".join(m["name"] for m in same)))

    @staticmethod
    def _write_session(path, account, doc, keep_bak=False):
        """写 CLI 登录态并回读校验；失败还原上一份，没有上一份就删掉半成品。"""
        bak = path + ".bak"
        had_old = os.path.isfile(path)
        if keep_bak and had_old:
            shutil.copy2(path, bak)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            write_json(path, doc)
            info = Client.session_summary(read_json(path), None, sealed=False)
            if info["uid"] != str(account.get("uid")):
                raise CoreError("写入后回读 uid 不一致")
            return info
        except Exception:
            if keep_bak and had_old and os.path.isfile(bak):
                shutil.copy2(bak, path)
            elif os.path.isfile(path):
                os.unlink(path)
            raise

    @classmethod
    def switch_cli(cls, account, backup_dir, log=None, slot=None):
        """把 CLI 槽位的登录态换成目标账号：默认槽位只停没人认领的 cbnode，命名实例只停自己的进程树。"""
        cls._guard()
        try:
            if bool(account.get("apiKey")):
                raise CoreError("静态 API Key 没有登录态，不能写进 CLI 槽位")
            if not slot and realm_of(account) != "intl":
                raise CoreError("默认 CLI 槽位只支持国际版账号；国内版请用命名实例")
            doc = Client.build_cli_session(account, Client.profiles(account))
            if slot:
                return cls._switch_named(account, doc, slot, log)
            slots = Slot()
            path = Client.cli_auth()
            if Process.cli_pids():
                Process.stop(cli=True, log=log, keep=slots.claimed_pids())
            with Snapshot.guarded(backup_dir, log=log):
                info = cls._write_session(path, account, doc)
            say(log, "CLI 槽位已写入 %s（%s），下次启动 CLI 生效"
                % (info["nickname"], str(info["uid"])[:8]))
            return {"uid": info["uid"], "nickname": info["nickname"]}
        finally:
            cls._lock.release()

    @classmethod
    def _switch_named(cls, account, doc, slot, log):
        """命名实例换号：停实例 → .bak 备份 → 写入校验 → 更新元数据 → 按需重启。"""
        slots = Slot()
        meta = slots.find(slot)
        if not meta:
            raise CoreError("没有这个实例：%s" % slot)
        cls._same_uid_warning(slots, account, slot, log)
        slots.ensure(slot)
        running = bool(meta.get("pids"))
        if running:
            slots.stop(meta, log=log)
        info = cls._write_session(slots.auth_path(slot), account, doc, keep_bak=True)
        meta.update({"uid": info["uid"], "nickname": info["nickname"],
                     "realm": realm_of(account)})
        slots.write(meta)
        if running:
            slots.launch(meta, log=log)
            say(log, "实例 %s 已换号并重启：%s" % (slot, info["nickname"]))
        else:
            say(log, "实例 %s 已换号为 %s，下次启动生效" % (slot, info["nickname"]))
        return {"uid": info["uid"], "nickname": info["nickname"], "slot": slot,
                "started": running}

    @classmethod
    def new_cli(cls, account, folder, log=None):
        """新建命名实例：建槽写入登录态并立即启动（文件夹由界面先选好）。"""
        cls._guard()
        try:
            if bool(account.get("apiKey")):
                raise CoreError("静态 API Key 没有登录态，不能建实例")
            doc = Client.build_cli_session(account, Client.profiles(account))
            slots = Slot()
            cls._same_uid_warning(slots, account, "", log)
            meta = slots.create(account, folder)
            try:
                info = cls._write_session(slots.auth_path(meta["name"]), account, doc)
                slots.launch(meta, log=log)
            except Exception:
                shutil.rmtree(slots.dir_of(meta["name"]), ignore_errors=True)
                raise
            say(log, "实例 %s 已建立并启动：%s @ %s" % (meta["name"], info["nickname"], folder))
            return {"uid": info["uid"], "nickname": info["nickname"],
                    "slot": meta["name"], "folder": folder, "started": True}
        finally:
            cls._lock.release()

    @classmethod
    def sync(cls, backup_dir, log=None):
        """把本地会话挂到当前登录账号名下，必要时重启客户端。"""
        cls._guard()
        try:
            uid = str(Client.current().get("uid") or "")
            if not uid:
                raise CoreError("读不到当前客户端登录账号")
            with cls._stopped(log) as running:
                with Snapshot.guarded(backup_dir, log=log):
                    result = Client.sync_sessions(uid, log=log)
                    if running:
                        Process.start(log=log)
                result.update({"uid": uid, "restarted": running, "restartRequired": not running})
                return result
        finally:
            cls._lock.release()

    @classmethod
    def restore(cls, backup_dir, log=None):
        """从备份恢复登录态与数据库。"""
        cls._guard()
        try:
            return Snapshot.restore(backup_dir, restart=True, log=log)
        finally:
            cls._lock.release()
