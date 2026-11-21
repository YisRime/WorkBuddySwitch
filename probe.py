"""账号体检：本地字段、凭证到期、上游连通与失效判定。

只读，不改账号：GET /v2/accounts 是「登录态还有没有效」最省事的一枪——401 / 403
就是失效，成功顺带拿回上游昵称，能纠正账号库里改过名的账号。结果按严重程度分四档：
ok / warn（能救） / expired（该续期） / invalid（要从上游重新登录）。
"""
import time

from util import CoreError, jwt_exp, norm_ts, realm_label

OK, WARN, EXPIRED, INVALID = "ok", "warn", "expired", "invalid"
LEVELS = {OK: "正常", WARN: "注意", EXPIRED: "过期", INVALID: "失效"}
RANK = {OK: 0, WARN: 1, EXPIRED: 2, INVALID: 3}


class Probe(object):
    """一个账号的体检结果与批量跑批。"""

    @staticmethod
    def local(entry, now=None):
        """本地检查（不联网）：返回 [(级别, 说明)]。"""
        now = now or time.time()
        issues = []
        token = str(entry.data.get("accessToken") or "")
        refresh = str(entry.data.get("refreshToken") or "")
        if not token and not entry.data.get("apiKey"):
            issues.append((INVALID, "没有 accessToken"))
        if not refresh and not entry.data.get("apiKey"):
            issues.append((WARN, "缺 refreshToken，凭证过期后只能重新登录"))
        stamp = norm_ts(entry.data.get("expiresAt")) or norm_ts(entry.expires_at)             or jwt_exp(token)
        if not stamp:
            issues.append((WARN, "没有到期时间（可能是手工导入的旧记录）"))
        elif stamp < now:
            issues.append((EXPIRED, "凭证已过期 %s" % _ago(stamp, now)))
        elif stamp - now < 3 * 86400:
            issues.append((WARN, "凭证 %s 后过期" % _left(stamp, now)))
        domain = str(entry.data.get("domain") or "").lower()
        realm = str(entry.data.get("realm") or "")
        if domain and realm:
            wants_cn = "codebuddy.cn" in domain or "tencent.com" in domain
            if wants_cn != (realm == "cn"):
                issues.append((WARN, "区域与域名不一致（记录 %s / 域名 %s）"
                               % (realm_label(realm), domain)))
        if entry.data.get("apiKey"):
            issues.append((WARN, "静态 API Key，没有登录态与活动权益"))
        return issues

    @classmethod
    def one(cls, entry, timeout=15):
        """联网体检一个账号：返回 {uid, level, issues, upstream}。"""
        issues = cls.local(entry)
        upstream = ""
        level = max((item[0] for item in issues), key=lambda value: RANK[value],
                    default=OK)
        if not entry.usable:
            return {"uid": entry.uid, "nickname": entry.nickname, "level": level,
                    "issues": [text for _level, text in issues], "upstream": ""}
        from client import Client
        try:
            rows = Client.profiles(entry.data)
            target = next((row for row in rows if str(row.get("uid")) == entry.uid), None)
            if target is None:
                issues.append((INVALID, "上游账号档案里没有这个 uid"))
            else:
                upstream = str(target.get("nickname") or "")
                if upstream and upstream != entry.nickname:
                    issues.append((WARN, "昵称与上游不一致：上游写的是「%s」" % upstream))
        except CoreError as exc:
            text = str(exc)
            if "HTTP 401" in text or "HTTP 403" in text:
                issues.append((INVALID, "上游拒绝（401/403）：凭证已失效，需重新登录"))
            elif "HTTP 400" in text:
                issues.append((WARN, "上游 400：%s" % _short(text)))
            else:
                issues.append((WARN, "上游暂时不可用：%s" % _short(text)))
        except Exception as exc:
            issues.append((WARN, "体检失败：%s" % _short(str(exc))))
        level = max((item[0] for item in issues), key=lambda value: RANK[value],
                    default=OK)
        return {"uid": entry.uid, "nickname": entry.nickname, "level": level,
                "issues": [text for _level, text in issues], "upstream": upstream}

    @classmethod
    def run(cls, entries, log=None):
        """批量体检（串行，避免把上游打出风控），返回 {uid: 结果} 与汇总。"""
        out, counts = {}, {OK: 0, WARN: 0, EXPIRED: 0, INVALID: 0}
        for entry in entries:
            try:
                row = cls.one(entry)
            except Exception as exc:
                row = {"uid": entry.uid, "nickname": entry.nickname, "level": WARN,
                       "issues": [str(exc)], "upstream": ""}
            out[entry.uid] = row
            counts[row["level"]] = counts.get(row["level"], 0) + 1
            if log:
                tail = ("：" + "；".join(row["issues"])) if row["issues"] else ""
                log("%s 体检%s%s" % (entry.nickname, LEVELS.get(row["level"], row["level"]),
                                    tail))
        return {"rows": out, "counts": counts, "total": len(entries)}


def _short(text, size=80):
    """错误文案压成一行。"""
    text = str(text or "").replace("\n", " ").strip()
    return text if len(text) <= size else text[:size] + "…"


def _left(stamp, now):
    """还剩多少天（保留一位）。"""
    return "%.1f 天" % ((stamp - now) / 86400.0)


def _ago(stamp, now):
    """已经过去多少天。"""
    return "%.1f 天" % ((now - stamp) / 86400.0)
