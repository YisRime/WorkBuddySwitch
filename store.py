"""账号库：data/Account 下一个账号一个 JSON，身份只认文件内容里的 uid。

文件名 `<昵称净化>-<uid 前 8 位>.json` 只是给人看的，改名不影响账号。旧版把账号塞在
一个 `workbuddy-accounts-*.json` 合集包里：只有 Account 目录还不存在时才拆一次，
之后完全以目录为准。
"""
import glob
import os
import re
import time

from util import (ACCOUNT_DIR, CoreError, earliest_expiring, norm_ts, read_json,
                  realm_of, write_json)


class Entry(object):
    """一条账号记录，连同它所在的文件与文件时间。"""

    __slots__ = ("data", "uid", "nickname", "realm", "access", "api_key",
                 "expires_at", "path", "file", "mtime", "credits", "last_checkin")

    def __init__(self, data, path, mtime=0.0):
        """从账号 JSON 取常用字段，缺昵称时退回 uid 前 8 位。"""
        self.data = data
        self.uid = str(data.get("uid") or "")
        self.nickname = str(data.get("nickname") or self.uid[:8] or "?")
        self.realm = realm_of(data)
        self.access = data.get("accessToken") if isinstance(data.get("accessToken"), str) else ""
        self.api_key = bool(data.get("apiKey"))
        self.expires_at = data.get("expiresAt") or 0
        self.path = path
        self.file = os.path.basename(path)
        self.mtime = mtime
        credits = data.get("credits")
        self.credits = credits if isinstance(credits, dict) else {}
        self.last_checkin = str(data.get("lastCheckin") or "")

    @property
    def usable(self):
        """能切换、能跑活动：有 accessToken 且不是静态 API Key。"""
        return bool(self.access) and not self.api_key

    @property
    def dead(self):
        """整行置灰：凭证缺失，或已经过期（过期还能靠续期救回来）。"""
        return not self.usable or 0 < norm_ts(self.expires_at) < time.time()


class Store(object):
    """data/Account 目录：读取、按 uid 去重、写回单个账号文件。"""

    def __init__(self, folder=None):
        """记住账号目录，并在内存里按 uid 缓存读到的账号。"""
        self.folder = os.path.abspath(folder or ACCOUNT_DIR)
        self.by_uid = {}

    def load(self):
        """读取全部账号，返回 (entries, notes)；同 uid 保留有凭证且较新的那份。"""
        self._migrate()
        errors, by_uid, pairs = [], {}, []
        for path in glob.glob(os.path.join(self.folder, "*.json")):
            try:
                pairs.append((path, os.path.getmtime(path)))
            except OSError:
                continue
        for path, mtime in sorted(pairs, key=lambda item: item[1]):
            name = os.path.basename(path)
            if name.lower() in ("settings.json", "active_realm.json",
                                "model_windows.json", "quota_scan.json"):
                continue
            try:
                row = read_json(path)
                if not isinstance(row, dict) or not str(row.get("uid") or ""):
                    raise ValueError("不是账号 JSON（缺 uid）")
            except Exception as exc:
                errors.append("%s：%s" % (name, exc))
                continue
            entry = Entry(row, path, mtime)
            old = by_uid.get(entry.uid)
            if old is None:
                by_uid[entry.uid] = entry
            elif entry.usable > old.usable or (entry.usable == old.usable
                                              and entry.mtime > old.mtime):
                errors.append("%s 与 %s 是同一个账号，保留 %s" % (old.file, name, name))
                by_uid[entry.uid] = entry
            else:
                errors.append("%s 与 %s 是同一个账号，保留 %s" % (name, old.file, old.file))
        self.by_uid = by_uid
        return sorted(by_uid.values(),
                      key=lambda e: (e.realm != "intl", e.nickname.lower(), e.uid)), errors

    def persist(self, entry):
        """把刷新过凭证的账号写回它自己的文件，返回文件路径。"""
        self._write(entry.path, entry.data)
        return entry.path

    def upsert(self, row):
        """收录一条账号：已有同 uid 就并进原文件，保留积分、档位等旧字段。"""
        uid = str((row or {}).get("uid") or "")
        if not uid:
            raise CoreError("账号记录缺少 uid，无法收录")
        entry = self.by_uid.get(uid)
        path = entry.path if entry else self._path_for(row)
        data = dict(entry.data) if entry else {}
        data.update(dict(row))
        self._write(path, data)
        self.by_uid[uid] = Entry(data, path, os.path.getmtime(path))
        return path

    def save_activity(self, uid, info):
        """把活动结果并回账号文件（credits / checkin / lastCheckin / 报告时间）；没有实际变化就不落盘。"""
        entry = self.by_uid.get(str(uid or ""))
        if not entry or not isinstance(info, dict):
            return False
        data, changed = entry.data, False
        remain, detail = info.get("remain"), info.get("detail")
        if remain is not None or detail:
            credits = data.get("credits") if isinstance(data.get("credits"), dict) else {}
            row = dict(credits)
            for key in ("remain", "size", "used"):
                if info.get(key) is not None:
                    row[key] = info[key]
            if detail:
                row["packages"] = detail
                first = earliest_expiring(detail)
                if first:
                    row["earliest_expiring"] = first
            if info.get("paid") is not None:
                row["is_paid_user"] = bool(info["paid"])
            size = row.get("size") or 0
            if size:
                row["used_percent"] = "%.1f%%" % (100.0 * (row.get("used") or 0) / size)
                row["remain_percent"] = "%.1f%%" % (100.0 * (row.get("remain") or 0) / size)
            if row != credits:
                row["updated_at"] = time.time()
                row["updated_iso"] = time.strftime("%Y-%m-%d %H:%M:%S")
                data["credits"], changed = row, True
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if info.get("checkin") == "已签到" and str(data.get("lastCheckin"))[:10] != stamp[:10]:
            data["lastCheckin"], changed = stamp, True
        if info.get("daily") and data.get("lastActivityReport") != stamp:
            data["lastActivityReport"], changed = stamp, True
        if changed:
            self._write(entry.path, data)
        return changed

    def _path_for(self, row):
        """按 `<昵称净化>-<uid 前 8 位>.json` 起名；撞名（同前缀不同 uid）加序号。"""
        base = re.sub(r'[\\/:*?"<>|]+', "_", str(row.get("nickname") or "").strip())
        base = base.strip(". ")[:48].rstrip(". ") or "account"
        stem = "%s-%s" % (base, str(row["uid"])[:8])
        path, index = os.path.join(self.folder, stem + ".json"), 1
        while os.path.isfile(path):
            index += 1
            path = os.path.join(self.folder, "%s-%d.json" % (stem, index))
        return path

    @staticmethod
    def _write(path, payload):
        """建好目录后把账号写成 JSON。"""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        write_json(path, payload)

    def _migrate(self):
        """Account 目录还不存在时，把旧版合集包拆成单账号文件（只做一次）。"""
        if os.path.isdir(self.folder):
            return
        for path in sorted(glob.glob(
                os.path.join(os.path.dirname(self.folder), "workbuddy-accounts-*.json"))):
            try:
                payload = read_json(path)
            except Exception:
                continue
            rows = payload.get("accounts") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict) and str(row.get("uid") or ""):
                    self._write(self._path_for(row), row)
