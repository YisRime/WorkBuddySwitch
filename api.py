"""上游接口：签到、每日活跃、积分额度、成长体系查询与任务流水（只打账号自己的接口）。

TASK_SPECS 按任务码给出事件类型 kind、目标次数与奖励，kind 决定点亮事件长什么样；
标了 unforgeable 的任务只能真实操作完成，DESKTOP_ONLY_TASKS 只认桌面端深链行为，
NIGHT_TASK_CODES 只在 23:00-08:00 上报才计数。专家 / 团队事件必须彼此不同 id：
上游按 (eventCode, id) 去重，重复 id 不推进进度。
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid

from util import (CoreError, REALMS, WEB_BASE, WEB_UA, as_dict, fmt_num, realm_of, say,
                  url_open, uuid7)


def _data(payload, *keys):
    """payload 的 data（可按 keys 继续下钻）；任一层不是 dict 就给空表。"""
    row = payload
    for key in ("data",) + keys:
        row = as_dict(row).get(key)
    return as_dict(row)


class Activity:
    """国内版 / 国际版的活动接口：计费额度、签到、成长任务与猫猫旅行。"""

    CHECKIN = "/v2/billing/meter/daily-checkin"
    CHECKIN_STATUS = "/v2/billing/meter/checkin-activity-status"
    RESOURCE = "/v2/billing/meter/get-user-resource"
    SUMMARY = "/billing/meter/get-user-resource-summary"
    FREE_PACKAGES = "/billing/meter/get-user-resource-free-packages"
    PAID_PACKAGES = "/billing/meter/get-user-resource-paid-packages"
    STREAK = "/activity/growth/streak"
    ENERGY = "/v2/activity/growth/energy"
    HEATMAP = "/activity/growth/heatmap"
    MAKEUP_USE = "/activity/growth/makeup-cards/use"
    REDEEM = "/activity/growth/redeem"
    LOTTERY = "/activity/growth/lottery/summary"
    LOTTERY_DRAW = "/activity/growth/lottery/draw"
    REPORT = "/v2/report"
    TASKS = "/v2/activity/growth/tasks"
    TASK_ACCEPT = "/v2/activity/growth/tasks/accept"
    TASK_CLAIM = "/activity/growth/tasks/%s/claim"
    TRAVEL_STATUS = "/activity/growth/buddy/travel/status"
    TRAVEL_CONFIG = "/activity/growth/buddy/travel/config"
    TRAVEL_DEPART = "/activity/growth/buddy/travel/depart"
    TRAVEL_CLAIM = "/activity/growth/buddy/travel/claim"

    @staticmethod
    def _derive(uid, salt):
        """按账号稳定派生 UUID 形状的设备标识（与 hub wb_fingerprint 同算法）。"""
        h = hashlib.md5(("%s:%s" % (salt, uid or "anonymous")).encode()).hexdigest()
        h = h[:12] + "4" + h[13:16] + "a" + h[17:]
        return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])

    @classmethod
    def billing_headers(cls, account):
        """计费 / 积分端点的出站头：国际版走 axios 形态，国内版走浏览器形态。"""
        realm = realm_of(account)
        cfg = REALMS[realm]
        uid = str(account.get("uid") or "")
        token = str(account.get("accessToken") or "")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg["billing_ua"],
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
            "Authorization": "Bearer " + token,
            "X-User-Id": uid,
            "X-Domain": str(account.get("domain") or cfg["domain"]),
            "X-CodeBuddy-Request": "1",
            "Accept-Language": "zh-CN" if realm == "cn" else "en-US",
            "X-Request-ID": uuid.uuid4().hex,
            "X-Machine-ID": cls._derive(uid, "machine"),
            "X-Session-ID": cls._derive(uid, "session"),
        }
        if realm == "cn":
            headers["X-Product"] = "SaaS"
            device = os.environ.get("WB_DEVICE_TOKEN") or ""
            if device:
                headers["X-Device-Token"] = device
        else:
            headers.update({"Accept": "application/json", "User-Agent": "axios/1.16.1",
                            "Accept-Encoding": "gzip, deflate"})
            for key in ("X-Requested-With", "Origin", "Referer"):
                headers.pop(key)
        if account.get("apiKey"):
            headers["X-API-Key"] = token
        if account.get("enterpriseId"):
            headers["X-Enterprise-Id"] = str(account["enterpriseId"])
            headers["X-Tenant-Id"] = str(account["enterpriseId"])
        else:
            headers["X-No-Enterprise-Id"] = "1"
        return headers

    @classmethod
    def chat_headers(cls, account):
        """对话 / 成长端点的出站头：对齐桌面端，带 UUIDv7 会话头族与链路头。"""
        realm = realm_of(account)
        cfg = REALMS[realm]
        uid = str(account.get("uid") or "")
        conv = str(uuid7())
        conv_req = uuid7().hex
        msg = uuid7().hex
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "x-codebuddy-request": "1",
            "X-Agent-Intent": "craft",
            "X-Agent-Purpose": "conversation",
            "X-Agent-Type": "main",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Version": cfg["version"],
            "X-Private-Data": "true",
            "X-Product": "SaaS",
            "X-Domain": str(account.get("domain") or cfg["domain"]),
            "User-Agent": cfg["ua"],
            "X-User-Id": uid,
            "Authorization": "Bearer " + str(account.get("accessToken") or ""),
            "X-Conversation-ID": conv,
            "X-Conversation-Request-ID": conv_req,
            "X-Root-Request-ID": conv_req,
            "X-Conversation-Message-ID": msg,
            "X-Request-ID": msg,
        }
        if realm != "cn":
            headers["Accept-Encoding"] = "gzip, deflate"
        trace, span = uuid.uuid4().hex, uuid.uuid4().hex[:16]
        headers.update({"X-Trace-ID": trace,
                        "traceparent": "00-%s-%s-01" % (trace, span),
                        "b3": "%s-%s-1" % (trace, span),
                        "X-B3-TraceId": trace, "X-B3-SpanId": span, "X-B3-Sampled": "1"})
        if account.get("enterpriseId"):
            headers["X-Enterprise-Id"] = str(account["enterpriseId"])
            headers["X-Tenant-Id"] = str(account["enterpriseId"])
        return headers

    @classmethod
    def _call(cls, url, headers, data=None, proxy="", timeout=20) -> "tuple[int, dict]":
        """返回 (HTTP 状态, JSON 对象)；不抛异常，签到回执里的 4xx 也是有效信息。"""
        request = urllib.request.Request(
            url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with url_open(request, proxy, timeout) as resp:
                return resp.status, cls._body(resp.read())
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            try:
                return exc.code, cls._body(body.encode("utf-8"))
            except ValueError:
                return exc.code, {"msg": body[:200]}
        except Exception as exc:
            return -1, {"msg": str(exc)}

    @staticmethod
    def _body(raw) -> dict:
        """响应体 → dict；不是 JSON 对象就抛 ValueError，交给调用方兜底。"""
        payload = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(payload, dict):
            raise ValueError("响应不是 JSON 对象")
        return payload

    @classmethod
    def _chat(cls, account, path, data=None, timeout=15) -> "tuple[int, dict]":
        """打国内版 chat 域的一个端点，返回 (状态, JSON)。"""
        cfg = REALMS["cn"]
        return cls._call(cfg["chat"] + path, cls.chat_headers(account), data,
                         str(account.get("proxy") or ""), timeout)

    @staticmethod
    def _msg(status, payload):
        """上游错误文案：优先 msg，没有就标 HTTP 状态。"""
        return str(payload.get("msg") or ("HTTP %s" % status))

    @staticmethod
    def _num(row, *keys):
        """按候选字段名取第一个能转成数字的值，都没有给 0。"""
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass
        return 0.0

    @classmethod
    def _package_rows(cls, account):
        """全部积分包：summary 只给包代码，名字 / 额度 / 到期要从 free / paid 补。"""
        cfg = REALMS[realm_of(account)]
        proxy = str(account.get("proxy") or "")
        headers = cls.billing_headers(account)
        status, payload = cls._call(cfg["billing"] + cls.SUMMARY, headers, b"{}", proxy, timeout=20)
        if payload.get("code") != 0:
            return [], {"error": cls._msg(status, payload)}
        summary = _data(payload)
        codes = [str(item["PackageCode"])
                 for item in (summary.get("Packages") or [])
                 if isinstance(item, dict) and item.get("PackageCode")]
        rows, seen = [], set()
        if codes:
            for path, states in ((cls.FREE_PACKAGES, [0]), (cls.PAID_PACKAGES, [0, 3])):
                body = json.dumps({"PackageCodes": codes, "PageNumber": 1, "PageSize": 200,
                                   "Status": states, "NeedRenewInfo": True}).encode("utf-8")
                _st, res = cls._call(cfg["billing"] + path, headers, body, proxy, timeout=25)
                for raw in (_data(res).get("Accounts") or []):
                    if not isinstance(raw, dict):
                        continue
                    key = raw.get("AccountId") or raw.get("ResourceId")
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    rows.append(cls._package(raw))
        return rows, {"paid": bool(summary.get("IsPaidUser"))}

    @staticmethod
    def _package(raw):
        """上游一个积分包的明细 → 账号文件里的 package 记录（字段与 hub 同口径）。"""
        size = Activity._num(raw, "CycleCapacitySizePrecise", "CycleCapacitySize", "CapacitySize")
        remain = Activity._num(raw, "CycleCapacityRemainPrecise", "CycleCapacityRemain",
                               "CapacityRemain")
        used = Activity._num(raw, "CycleCapacityUsedPrecise", "CycleCapacityUsed", "CapacityUsed")
        if size > 0 and used <= 0 and remain <= size:
            used = max(0.0, size - remain)
        elif size > 0 and remain <= 0 and used < size:
            remain = max(0.0, size - used)
        reason = ""
        for attr in (raw.get("AccountAttributes") or []):
            if isinstance(attr, dict) and attr.get("Key") == "grantReason":
                reason = str(attr.get("Value") or "")
                break
        created = ""
        try:
            stamp = float(raw.get("CreateTime") or 0)
            if stamp > 1e11:
                stamp /= 1000.0
            if stamp:
                created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
        except (TypeError, ValueError):
            created = str(raw.get("CreateTime") or "")
        cycle_end = str(raw.get("CycleEndTime") or raw.get("ExpiredTime") or "")
        expire, days, expired, forever = cycle_end, None, False, False
        try:
            stamp = float(raw.get("DeductionEndTime") or 0)
            if stamp > 1e11:
                stamp /= 1000.0
            if stamp:
                expire = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
        except (TypeError, ValueError):
            pass
        if expire:
            try:
                left = time.mktime(time.strptime(expire.replace("T", " ")[:19],
                                                 "%Y-%m-%d %H:%M:%S")) - time.time()
                if left > 3650 * 86400:
                    forever = True
                else:
                    days, expired = round(left / 86400.0, 1), left < 0
            except (ValueError, OSError):
                pass
        in_use = bool(raw.get("InUsage"))
        if not in_use and not expired and remain > 0 and used > 0:
            in_use = True
        monthly = bool(forever) or (bool(expire) and str(expire)[:4].isdigit()
                                    and int(str(expire)[:4]) >= 2030)
        return {
            "name": str(raw.get("PackageName") or "Package"),
            "package_code": str(raw.get("PackageCode") or ""),
            "product_name": str(raw.get("ProductName") or ""),
            "sub_product_name": str(raw.get("SubProductName") or ""),
            "grant_reason": reason,
            "resource_id": str(raw.get("ResourceId") or ""),
            "deal_name": str(raw.get("DealName") or ""),
            "create_time": created,
            "remain": round(remain, 2),
            "used": round(used, 2),
            "size": round(size, 2),
            "unit": str(raw.get("CapacityUnit") or "credits"),
            "in_usage": in_use,
            "auto_renew": bool(raw.get("AutoRenewFlag") or raw.get("SupportAutoRenew")),
            "cycle_start_time": str(raw.get("CycleStartTime") or ""),
            "cycle_end_time": cycle_end,
            "expire_time": expire,
            "no_expiry": forever,
            "monthly": monthly,
            "days_left": days,
            "is_expired": expired,
            "status": raw.get("Status", 0),
        }

    @classmethod
    def credits(cls, account):
        """积分 / 额度：国内版走「摘要 + free / paid 明细」，国际版一次取全。"""
        realm = realm_of(account)
        cfg = REALMS[realm]
        proxy = str(account.get("proxy") or "")
        body = json.dumps({
            "PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca", "Status": [0, 3],
            "PackageEndTimeRangeBegin": time.strftime("%Y-%m-%d %H:%M:%S"),
            "PackageEndTimeRangeEnd": "2036-01-01 00:00:00",
        }).encode("utf-8")
        status, payload = cls._call(cfg["billing"] + cls.RESOURCE,
                                    cls.billing_headers(account), body, proxy, timeout=25)
        if payload.get("code") != 0:
            return {"ok": False, "error": cls._msg(status, payload)}
        data = _data(payload, "Response", "Data")
        packages = [cls._package(row) for row in (data.get("Accounts") or [])
                    if isinstance(row, dict)]
        paid = None
        if realm == "cn":
            detail, extra = cls._package_rows(account)
            if detail:
                packages = detail
            paid = extra.get("paid")

        packages.sort(key=lambda item: (
            0 if item.get("in_usage") else 1,
            1 if item.get("is_expired") else 0,
            item.get("days_left") if item.get("days_left") is not None else 99999,
            -item.get("remain", 0)))
        remain = round(sum(item["remain"] for item in packages), 2)
        size = round(sum(item["size"] for item in packages), 2)
        used = round(sum(item["used"] for item in packages), 2)
        return {"ok": True, "remain": remain, "size": size, "used": used,
                "packages": len(packages), "detail": packages,
                "paid": paid, "updated": time.strftime("%H:%M:%S")}

    @classmethod
    def checkin_status(cls, account):
        """签到状态（chat 域只读）：今日是否已签、连登天数、每日 / 今日积分。"""
        if realm_of(account) != "cn":
            return {}
        cfg = REALMS["cn"]
        _status, payload = cls._call(cfg["chat"] + cls.CHECKIN_STATUS,
                                     cls.billing_headers(account), b"{}",
                                     str(account.get("proxy") or ""), timeout=10)
        data = _data(payload)
        if not data:
            return {}
        return {"checked": bool(data.get("today_checked_in")),
                "days": int(data.get("streak_days") or 0),
                "daily_credit": int(data.get("daily_credit") or 0),
                "today_credit": int(data.get("today_credit") or 0)}

    @classmethod
    def growth(cls, account):
        """连登 / 能量 / 抽奖次数 / 今日签到状态（只有国内版有这套成长体系）。"""
        if realm_of(account) != "cn":
            return {"ok": False, "error": "仅国内版"}
        out = {"ok": True, "updated": time.strftime("%H:%M:%S")}
        status, payload = cls._chat(account, cls.STREAK, None, 10)
        data = _data(payload)
        if data:
            out["days"] = int(as_dict(data.get("streak")).get("days") or 0)
        else:
            out["ok"] = False
            out["error"] = str(payload.get("msg") or status)
        data = _data(cls._chat(account, cls.ENERGY, None, 10)[1])
        if data:
            out["energy"] = int(data.get("balance") or 0)
        data = _data(cls._chat(account, cls.LOTTERY, None, 10)[1])
        if data:
            out["chances"] = int(data.get("chances") or 0)
        info = cls.checkin_status(account)
        if info:
            out["checked"] = info["checked"]
            out["today_credit"] = info["today_credit"]
            out.setdefault("days", info["days"])
        return out

    @classmethod
    def tasks(cls, account):
        """成长任务列表（含进度、奖励与深链里的专家 id）。"""
        if realm_of(account) != "cn":
            return []
        status, payload = cls._chat(account, cls.TASKS)
        rows = []
        for task in (_data(payload).get("tasks") or []):
            code = str(task.get("task_code") or "") if isinstance(task, dict) else ""
            if not code:
                continue
            progress = as_dict(task.get("progress"))
            jump = str(task.get("jump_url") or "")
            found = re.search(r"expertId(?:=|%3D)([A-Za-z0-9_]+)", jump, re.I)
            expert = None
            if found:
                label = re.search(r"「(.+?)」", str(task.get("title") or task.get("task_desc") or ""))
                expert = (found.group(1), label.group(1) if label else "")
            rows.append({
                "code": code,
                "name": str(task.get("title") or code),
                "desc": str(task.get("description") or task.get("task_desc") or ""),
                "status": str(task.get("accept_status") or "not_accepted"),
                "type": str(task.get("task_type") or ""),
                "current": int(progress.get("current") or 0),
                "target": int(progress.get("target") or 1),
                "credit": int(task.get("reward_credit") or 0),
                "energy": int(task.get("reward_energy") or 0),
                "jump": jump,
                "expert": expert,
            })
        return rows

    @staticmethod
    def _done_today(account, *keys):
        """账号文件里今天已经记过这件事（与 hub 的 can_checkin / can_daily_chat 同口径）。"""
        today = time.strftime("%Y-%m-%d")
        return any(str(account.get(key) or "")[:10] == today for key in keys)

    @classmethod
    def checkin(cls, account, log=None, force=False):
        """Buddy 加油站签到；当天签过就不再打一次上游（force 可强制重签）。"""
        if realm_of(account) != "cn":
            raise CoreError("签到只对国内版账号开放")
        if account.get("apiKey"):
            raise CoreError("静态 API Key 没有签到权益")
        if account.get("enterpriseId"):
            raise CoreError("企业号不支持签到")
        name = account.get("nickname") or str(account.get("uid"))[:8]
        if not force and cls._done_today(account, "lastCheckin"):
            say(log, "%s Buddy 加油站签到：今天已签到，跳过" % name)
            return {"ok": True, "state": "already", "msg": "今天已签到",
                    "at": time.strftime("%H:%M:%S")}
        cfg = REALMS["cn"]
        status, payload = cls._call(cfg["billing"] + cls.CHECKIN, cls.billing_headers(account),
                                    b"{}", str(account.get("proxy") or ""), timeout=15)
        code = payload.get("code", -1)
        msg = str(payload.get("msg") or status)
        if any(k in msg for k in ("处理中", "请勿重复操作", "系统繁忙", "稍后再试")):
            state = "pending"
        elif code == 0:
            state = "failed" if "失败" in msg else "checked"
        elif code == 10001:
            state = "already" if "已签到" in msg else "failed"
        else:
            state = "failed"
        say(log, "%s Buddy 加油站签到：%s" % (name, msg))
        return {"ok": state in ("checked", "already"), "state": state, "msg": msg,
                "at": time.strftime("%H:%M:%S")}

    @classmethod
    def report(cls, account, log=None, force=False):
        """上报一条 chat 事件点亮连登（每号每天一次，与客户端口径一致）。"""
        if realm_of(account) != "cn":
            raise CoreError("每日活跃只对国内版开放")
        if account.get("apiKey") or account.get("enterpriseId"):
            raise CoreError("该账号没有成长体系")
        name = account.get("nickname") or str(account.get("uid"))[:8]
        if not force and cls._done_today(account, "lastActivityReport", "lastDailyChat"):
            say(log, "%s 每日活跃：今天已上报，跳过" % name)
            return {"ok": True, "skipped": True, "msg": "今天已上报",
                    "at": time.strftime("%H:%M:%S")}
        now = int(time.time() * 1000)
        cid = "wb-task-%d-0" % now
        event = {
            "eventCode": "chat_request_send", "timestamp": now, "reportDelay": 0,
            "mode": "craft", "conversationId": cid, "requestId": cid + "-req",
            "inputLength": 12, "requestModelId": "deepseek-v4-flash",
            "requestModelName": "DeepSeek V4 Flash", "isPlan": False,
            "agentName": "default", "agentType": "conversation",
            "userId": str(account.get("uid") or ""),
        }
        status, payload = cls._chat(account, cls.REPORT,
                                    json.dumps([event]).encode("utf-8"))
        ok = payload.get("code") == 0
        msg = str(payload.get("msg") or status)
        days = None
        if ok:
            data = _data(cls._chat(account, cls.STREAK, None, 10)[1])
            if data:
                days = int(as_dict(data.get("streak")).get("days") or 0)
        tail = ""
        if ok and days is not None:
            tail = ("（连续打卡 %d 天）" % days if days
                    else "（连登仍是 0 天，可能被上游静默丢弃）")
        say(log, "%s 每日活跃：%s%s" % (name, msg, tail))
        return {"ok": ok, "msg": msg, "days": days, "at": time.strftime("%H:%M:%S")}

    @classmethod
    def accept(cls, account, codes):
        """批量接取任务（每 20 个一批）；结果明细由调用方 Growth 负责打日志。"""
        codes = [str(code) for code in codes if code]
        out = {"ok": True, "accepted": [], "failed": [], "msg": "", "reasons": []}
        for start in range(0, len(codes), 20):
            part = codes[start:start + 20]
            status, payload = cls._chat(account, cls.TASK_ACCEPT,
                                        json.dumps({"task_codes": part}).encode("utf-8"))
            if payload.get("code") != 0:
                out["ok"] = False
                out["msg"] = cls._msg(status, payload)
                out["failed"].extend(part)
                out["reasons"].append(out["msg"])
                continue
            seen = set()
            for item in (_data(payload).get("results") or []):
                if not isinstance(item, dict):
                    continue
                code = str(item.get("task_code") or "")
                seen.add(code)
                if str(item.get("status") or "") in ("accepted", "already_accepted"):
                    out["accepted"].append(code)
                else:
                    out["failed"].append(code)
                    out["reasons"].append("%s: %s" % (code, item.get("message") or item.get("status")))
            out["failed"].extend(code for code in part if code not in seen)
        return out

    @classmethod
    def claim(cls, account, code):
        """领取单个任务奖励；桌面域 400 时降级到网页域。"""
        status, payload = cls._chat(account, cls.TASK_CLAIM % code, b"{}")
        if payload.get("code") != 0 and status == 400:
            status, payload = cls._call(
                WEB_BASE + cls.TASK_CLAIM % code,
                {"Authorization": "Bearer " + str(account.get("accessToken") or ""),
                 "Accept": "application/json, text/plain, */*",
                 "Content-Type": "application/json",
                 "Origin": WEB_BASE, "Referer": WEB_BASE + "/profile/growth-center",
                 "x-client-platform": "web", "User-Agent": WEB_UA,
                 "X-User-Id": str(account.get("uid") or ""), "X-Domain": WEB_BASE},
                b"{}", str(account.get("proxy") or ""))
        if payload.get("code") == 0:
            data = _data(payload)
            return {"ok": True, "credit": int(data.get("credit") or 0),
                    "energy": int(data.get("energy") or 0)}
        return {"ok": False, "msg": cls._msg(status, payload)}

    @classmethod
    def lottery(cls, account):
        """把当前抽奖次数一次抽完，返回实际抽中的次数。"""
        _status, payload = cls._chat(account, cls.LOTTERY, None, 10)
        chances = int(_data(payload).get("chances") or 0)
        drawn = 0
        for _index in range(chances):
            body = json.dumps({"client_token": str(uuid.uuid4())}).encode("utf-8")
            _status, res = cls._chat(account, cls.LOTTERY_DRAW, body)
            if res.get("code") != 0:
                break
            drawn += 1
            time.sleep(1.0)
        return drawn

    @classmethod
    def travel_status(cls, account):
        """旅行状态；查询失败抛 CoreError。"""
        status, payload = cls._chat(account, cls.TRAVEL_STATUS, None, 10)
        data = _data(payload)
        if not data:
            raise CoreError(cls._msg(status, payload))
        return data

    @classmethod
    def travel_config(cls, account):
        """旅行目的地清单。"""
        _status, payload = cls._chat(account, cls.TRAVEL_CONFIG, None, 10)
        return _data(payload).get("locations") or []

    @classmethod
    def travel_depart(cls, account, location_id):
        """派出旅行；返回 (data, err)。"""
        body = json.dumps({"location_id": location_id}).encode("utf-8")
        status, payload = cls._chat(account, cls.TRAVEL_DEPART, body, 10)
        if payload.get("code") == 0:
            return _data(payload), None
        return None, cls._msg(status, payload)

    @classmethod
    def travel_claim(cls, account):
        """领取旅行归来奖励；返回 (ok, credit, msg)。"""
        status, payload = cls._chat(account, cls.TRAVEL_CLAIM, b"{}", 10)
        if payload.get("code") == 0:
            data = _data(payload)
            return True, int(data.get("reward_credit") or 0), ""
        return False, 0, cls._msg(status, payload)


TASK_SPECS = {
    "create_canvas": {"kind": "canvas", "target": 1, "reward": 300, "name": "创建设计任务"},
    "template_5": {"kind": "template", "target": 5, "reward": 200, "name": "模板创建任务"},
    "expert_5": {"kind": "expert", "target": 5, "reward": 200, "name": "使用专家助手"},
    "Expert_team_use_3": {"kind": "team", "target": 3, "reward": 150, "name": "使用专家团队"},
    "skill_1": {"kind": "skill", "target": 1, "reward": 100, "name": "体验技能"},
    "automation_1": {"kind": "automation", "target": 1, "reward": 100, "name": "创建自动化任务"},
    "playbook_prompt": {"kind": "playbook", "target": 1, "reward": 100, "name": "灵感案例使用"},
    "Expert_lighthouse": {"kind": "lighthouse", "target": 1, "reward": 100, "name": "轻量云专家使用"},
    "Buddy_App": {"kind": "buddy5", "target": 1, "reward": 100, "name": "进入 Buddy 应用"},
    "Buddy_App_QQ": {"kind": "buddy5", "target": 1, "reward": 100, "name": "企鹅教师助手"},
    "Hp_Appearance": {"kind": "skin", "target": 1, "reward": 100, "name": "应用主题外观"},
    "chat_5": {"kind": "chat", "target": 5, "reward": 100, "name": "发起 5 次对话"},
    "Model_chat_GLM5.2": {"kind": "glmchat", "target": 1, "reward": 100, "name": "体验 GLM-5.2"},
    "black_cat": {"kind": "cat", "target": 3, "reward": 100, "name": "夜猫子任务 (23:00-08:00)"},
    "RichMeow_Chat": {"kind": "richmeow", "target": 1, "reward": 100, "name": "桌面对话事件链"},
    "Library_read": {"kind": "library", "target": 1, "reward": 100, "name": "浏览资料库"},
    "first_buddy": {"kind": "chat", "target": 1, "reward": 300, "name": "领取一只 Buddy"},
    "WorkBuddy_Valorant": {"kind": "expert", "target": 1, "reward": 100,
                           "expert": ("ex_xQHHD1u5Jkua", "无畏契约拼豆专家"),
                           "name": "体验「无畏契约拼豆专家」"},
    "wb_wechat_oa_subscribe_task": {"unforgeable": True, "reward": 100,
                                    "reason": "需在微信关注「腾讯 WorkBuddy」公众号满 24 小时",
                                    "name": "关注官方公众号"},
    "Expert_Philanthropy": {"unforgeable": True, "reason": "真实捐款动作", "reward": 0,
                            "name": "公益爱心捐赠"},
}

DESKTOP_ONLY_TASKS = {
    "RichMeow_Chat": "在桌面端发起 1 次对话",
    "Library_read": "在桌面端打开「资料库」并读完介绍文档",
    "Buddy_App": "在桌面端左上角「发现应用」进入任意一个 Buddy 应用",
    "Buddy_App_QQ": "在桌面端「发现应用」进入「企鹅教师助手」",
}

NIGHT_TASK_CODES = {"black_cat"}

EXPERT_ID_POOL = [
    ("ex_PZw8Gu81HfN4", "运维工程师"), ("ex_ROsDtJbzADFV", "产品经理"),
    ("ex_SMUnl0nJbPix", "UI设计师"), ("ex_ZTR062oVBOCW", "数据分析师"),
    ("ex_a3sSSFBy8qaC", "后端架构师"), ("ex_aG1kvKbq8lPx", "文案策划"),
    ("ex_al1vxtUOYQ10", "测试专家"), ("ex_cZfiyuET9UQP", "安全顾问"),
    ("ex_eggOvQuVP0hq", "算法工程师"), ("ex_hSwsQjkSKnkX", "前端工程师"),
    ("ex_mMbwwmFA9n9P", "项目管理专家"), ("ex_uAQE5POfk7Zh", "增长运营专家"),
    ("ex_uZzSAScSy7FZ", "行业研究员"), ("ex_LHywGrZOtG7G", "数据分析师"),
    ("ex_NX5C8GBciVed", "测试架构师"), ("ex_DdCsaoq4AtcO", "云端运维专家"),
    ("ex_KzqKQguubrNQ", "内容创作专家"), ("ex_2cvvUZQhDyeJ", "腾讯轻量云专家"),
]
TEAM_ID_POOL = [
    ("CloudOpsTeam", "运维专家团队"), ("CloudContentTeam", "内容专家团队"),
    ("CloudDevTeam", "研发专家团队"), ("ProductStrategyTeam", "产品战略团队"),
    ("MarketingCampaignTeam", "营销活动团队"), ("SalesBattleTeam", "销售作战团队"),
    ("DesignEngineTeam", "设计引擎团队"), ("HrOperationsTeam", "人力运营团队"),
]


class Growth(object):
    """一次成长任务流水的执行器；全过程写日志，不碰界面。"""

    GAP = 1.0

    @staticmethod
    def build_event(uid, kind, idx=0, expert=None):
        """构造指定类型的规范事件（与桌面客户端上报口径一致）。"""
        now = int(time.time() * 1000)
        cid = "wb-task-%d-%d" % (now, idx)
        rid = "%s-req" % cid
        if kind == "canvas":
            return {"eventCode": "wbx_design_canvas_task_create", "timestamp": now,
                    "reportDelay": 0, "conversationId": cid, "requestId": rid,
                    "source": "summon_keyword", "isCustomModel": False, "name": "",
                    "inputLength": 12, "id": "wbx-canvas-%d" % now, "cost": 0,
                    "isSuccessful": True, "userId": uid}
        if kind == "template":
            return {"eventCode": "agent_task_created_with_template", "timestamp": now,
                    "reportDelay": 0, "isCustomModel": True, "id": str(idx),
                    "name": "幻灯片", "requestId": rid, "conversationId": cid, "userId": uid}
        if kind in ("expert", "team", "lighthouse"):
            if expert:
                ex_id, name = expert
            elif kind == "lighthouse":
                ex_id, name = "ex_2cvvUZQhDyeJ", "腾讯轻量云专家"
            elif kind == "team":
                ex_id, name = "CloudOpsTeam", "运维专家团队"
            else:
                ex_id, name = "ContentCreator", "内容创作专家"
            return {"eventCode": "expert_actual_use", "timestamp": now, "reportDelay": 0,
                    "mode": "CLOUD", "id": ex_id, "name": name, "expertTitle": name,
                    "type": "02-Engineering", "expertType": "team" if kind == "team" else "agent",
                    "source": "builtin", "version": "1.0.2", "cost": 0, "characterCount": 12,
                    "conversationId": cid, "requestId": rid, "messageId": rid,
                    "requestModelId": "deepseek-v4-flash",
                    "requestModelName": "DeepSeek V4 Flash", "userId": uid}
        if kind == "skill":
            return {"eventCode": "skill_info", "timestamp": now, "reportDelay": 0,
                    "skillId": "skill_2096525080079265792", "name": "pptx", "userId": uid}
        if kind == "automation":
            return {"eventCode": "automated_task_create_suc", "timestamp": now,
                    "reportDelay": 0, "name": "每周工作整理", "type": "cron",
                    "source": "manually", "modelId": "deepseek-v4-flash",
                    "modelIsThinking": False, "conversationId": cid, "requestId": rid,
                    "schedule": {"type": "recurring",
                                 "rrule": "FREQ=WEEKLY;BYDAY=FR;BYHOUR=9;BYMINUTE=0"},
                    "prompt": "每周五自动整理本周工作", "userId": uid}
        if kind == "playbook":
            return {"eventCode": "playbook_prompt_send", "timestamp": now,
                    "reportDelay": 0, "id": "worker-ledger-freedom-dashboard",
                    "name": "打工人小账本", "type": "other", "promptLength": 10,
                    "isOfficial": 1, "source": "discover", "conversationId": cid,
                    "requestId": rid, "userId": uid}
        if kind == "skin":
            return {"eventCode": "appearance_skin_apply", "timestamp": now,
                    "reportDelay": 0, "action": "apply", "source": "settings_close",
                    "id": "theme-tkmw7j", "vipLevel": "free", "series": "craft",
                    "type": "unknown", "name": "和平精英激战金秋", "userId": uid}
        if kind in ("chat", "glmchat", "cat"):
            glm = kind in ("glmchat", "cat")
            return {"eventCode": "chat_request_send", "timestamp": now, "reportDelay": 0,
                    "mode": "night" if kind == "cat" else "craft",
                    "conversationId": cid, "requestId": rid, "inputLength": 12,
                    "requestModelId": "glm-5.2" if glm else "deepseek-v4-flash",
                    "requestModelName": "GLM-5.2" if glm else "DeepSeek V4 Flash",
                    "isPlan": False, "agentName": "default", "agentType": "conversation",
                    "userId": uid}
        return {"eventCode": "heartbeat", "timestamp": now, "userId": uid}

    @classmethod
    def _claim(cls, account, code, log, name, done="", fail=""):
        """领奖并写一行日志；返回本次到账积分。"""
        res = Activity.claim(account, code)
        if res["ok"]:
            say(log, "✓ 任务 [%s] %s领奖成功: +%d 积分" % (name, done, res["credit"]))
            time.sleep(cls.GAP)
            return res["credit"]
        say(log, "! 任务 [%s] %s: %s" % (name, fail or "领奖失败", res["msg"] or "未知原因"))
        time.sleep(cls.GAP)
        return 0

    @classmethod
    def _one(cls, account, task, log):
        """推进单个任务并返回本次到账积分；auto 类不用接取，可直接点亮。"""
        code = task["code"]
        spec = TASK_SPECS.get(code)
        if not spec and task.get("expert"):
            spec = {"kind": "expert", "target": task["target"] or 1,
                    "name": task["name"], "expert": task["expert"]}
        if not spec or spec.get("unforgeable"):
            if spec and spec.get("reason"):
                say(log, "⏭ 任务 [%s] 需真实操作完成: %s, 跳过"
                    % (spec.get("name") or code, spec["reason"]))
            elif not spec:
                say(log, "⏭ 任务 [%s] 没有可伪造的事件类型, 跳过" % task["name"])
            return 0
        name = spec["name"]
        status, cur, tgt = task["status"], task["current"], task["target"]
        if status == "claimed":
            return 0
        if status == "completed" or cur >= tgt:
            return cls._claim(account, code, log, name)
        if code in DESKTOP_ONLY_TASKS:
            say(log, "⏭ 任务 [%s] 需真实操作完成: %s (深链 %s), 跳过事件伪造"
                % (name, DESKTOP_ONLY_TASKS[code], task.get("jump") or "workbuddy://chat"))
            return 0
        if code in NIGHT_TASK_CODES and 8 <= time.localtime().tm_hour < 23:
            say(log, "🌙 任务 [%s] 仅 23:00-08:00 上报计数, 当前不在窗口, 跳过"
                     " (到点再点一次「任务」, 累计 3 天)" % name)
            return 0
        if status == "not_accepted" and task["type"] != "auto":
            say(log, "⏭ 任务 [%s] 仍未接取, 跳过 (先解决接取失败)" % name)
            return 0
        need = max(1, tgt - cur)
        kind = spec.get("kind")
        say(log, "正在点亮任务 [%s] (需上报 %d 次)..." % (name, need))
        pool = TEAM_ID_POOL if kind == "team" else (
            EXPERT_ID_POOL if kind == "expert" else None)
        report_ok = True
        for i in range(need):
            expert = spec.get("expert")
            if expert is None and pool:
                expert = pool[(cur + i) % len(pool)]
            event = cls.build_event(account.get("uid"), kind, i, expert)
            _status, res = Activity._chat(account, Activity.REPORT,
                                          json.dumps([event]).encode("utf-8"))
            if res.get("code") != 0:
                report_ok = False
            if i < need - 1:
                time.sleep(cls.GAP)
        if not report_ok:
            say(log, "! 任务 [%s] 部分事件上报失败 (上游拒绝), 继续尝试领奖" % name)
        time.sleep(1.5)
        prog = cur
        for attempt in range(6):
            fresh = next((t for t in Activity.tasks(account) if t["code"] == code), None)
            if fresh:
                prog = fresh["current"]
                if prog >= tgt or fresh["status"] in ("completed", "claimed"):
                    break
                if prog > cur:
                    time.sleep(2.5)
                    continue
            if attempt < 2:
                time.sleep(1.5)
            else:
                break
        if prog < tgt:
            say(log, "? 任务 [%s] 已上报但进度 %d/%d 未达成, 领奖顺延到下次运行"
                % (name, prog, tgt))
            time.sleep(cls.GAP)
            return 0
        return cls._claim(account, code, log, name, done="点亮并",
                          fail="进度已达 %d/%d 但领奖失败" % (prog, tgt))

    @classmethod
    def _need_accept(cls, task):
        """这个任务是否还需要接取：未接取、可伪造、且不是 auto 类。"""
        spec = TASK_SPECS.get(task["code"]) or {}
        return (task["status"] == "not_accepted" and not spec.get("unforgeable")
                and task["type"] != "auto")

    @classmethod
    def run(cls, account, log=None):
        """完整流水：auto 任务 → 批量接取 → 逐个点亮领取 → 猫猫日常 → 刷新余额。"""
        if realm_of(account) != "cn":
            say(log, "国际版不适用国内成长任务中心")
            return {"ok": False, "credit": 0}
        if account.get("enterpriseId") or account.get("apiKey"):
            say(log, "企业版账号无成长体系，跳过")
            return {"ok": True, "credit": 0}
        name = account.get("nickname") or str(account.get("uid"))[:8]
        say(log, "开始为账号 %s 运行成长任务自动化..." % name)
        tasks = Activity.tasks(account)
        if not tasks:
            say(log, "未能获取到任务清单，请检查网络或账号状态")
            return {"ok": False, "credit": 0}
        earned = 0
        for task in tasks:
            if task["type"] == "auto":
                earned += cls._one(account, task, log)
        pending = [task["code"] for task in tasks if cls._need_accept(task)]
        if pending:
            say(log, "发现 %d 个待接取任务，正在批量接取..." % len(pending))
            acc = Activity.accept(account, pending)
            if acc["failed"]:
                why = acc["msg"] or "; ".join(acc["reasons"])[:160]
                head = ", ".join(acc["failed"][:5]) + (" ..." if len(acc["failed"]) > 5 else "")
                say(log, "! 接取未成功 %d 个: %s%s"
                    % (len(acc["failed"]), head, " (%s)" % why if why else ""))
            if acc["accepted"]:
                say(log, "✓ 已接取 %d 个任务" % len(acc["accepted"]))
            time.sleep(cls.GAP)
            tasks = Activity.tasks(account)
            still = [task["code"] for task in tasks if cls._need_accept(task)]
            if still:
                say(log, "仍有 %d 个未接取，重试接取一次..." % len(still))
                retry = Activity.accept(account, still)
                if retry["accepted"]:
                    say(log, "✓ 重试接取成功 %d 个" % len(retry["accepted"]))
                time.sleep(cls.GAP)
                tasks = Activity.tasks(account)
            if not tasks:
                say(log, "! 接取后无法获取任务清单，本轮中止")
                return {"ok": False, "credit": earned}
            stuck = [task["code"] for task in tasks if cls._need_accept(task)]
            if stuck:
                say(log, "! 仍有 %d 个任务处于未接取状态，"
                         "对未接取任务上报事件不会计入进度，本轮跳过这些任务" % len(stuck))
        for task in tasks:
            if task["type"] != "auto":
                earned += cls._one(account, task, log)
        travel = cls.travel(account)
        if travel.get("msg"):
            say(log, "猫猫日常: %s" % travel["msg"])
        earned += travel.get("credit") or 0
        snap = cls.snapshot(account)
        if snap.get("remain") is not None:
            say(log, "🎉 全部完成！本次累计新增到账: +%d 积分，当前总剩余: %s 积分"
                % (earned, fmt_num(snap["remain"], 0)))
        else:
            say(log, "🎉 全部完成！本次累计新增到账: +%d 积分" % earned)
        return {"ok": True, "credit": earned, "activity": snap}

    @classmethod
    def ensure_buddy(cls, account, log=None):
        """没有 Buddy 实例时，完成并领取 first_buddy（奖励即 Buddy 实例）。"""
        tasks = Activity.tasks(account)
        buddy = next((t for t in tasks if t["code"] == "first_buddy"), None)
        if buddy is None:
            return False
        if buddy["status"] == "claimed":
            return True
        cls._one(account, buddy, log)
        tasks = Activity.tasks(account)
        buddy = next((t for t in tasks if t["code"] == "first_buddy"), None)
        return bool(buddy and buddy["status"] == "claimed")

    @classmethod
    def travel(cls, account, log=None):
        """猫猫旅行：归来领奖 / 在家派出；没有 Buddy 时先补 first_buddy。"""
        if realm_of(account) != "cn":
            return {"ok": False, "msg": "国际版不适用国内成长任务中心"}
        if account.get("enterpriseId") or account.get("apiKey"):
            return {"ok": True, "action": "skip", "msg": "企业版账号无成长体系，跳过"}
        try:
            state = Activity.travel_status(account)
        except Exception as exc:
            return {"ok": False, "msg": "查询旅行状态失败: %s" % exc}
        mode = str(state.get("state") or "")
        if mode == "arrived":
            ok, credit, msg = Activity.travel_claim(account)
            if ok:
                return {"ok": True, "action": "claim", "credit": credit,
                        "msg": "旅行归来领奖成功！获得 %d 积分" % credit}
            return {"ok": False, "msg": "领奖失败: %s" % msg}
        if mode == "idle":
            if state.get("daily_limit_reached"):
                return {"ok": True, "action": "limit",
                        "msg": "猫猫今日已完成旅行，明日 00:00 刷新"}
            lid = 1
            try:
                locations = Activity.travel_config(account)
                if locations:
                    lid = as_dict(locations[0]).get("id") or 1
            except Exception:
                pass
            data, err = Activity.travel_depart(account, lid)
            if data is None and "no active buddy" in str(err or "").lower():
                if cls.ensure_buddy(account, log):
                    data, err = Activity.travel_depart(account, lid)
                else:
                    return {"ok": False,
                            "msg": "还没有 Buddy：需先完成并领取「领取一只 Buddy」任务"
                                   "（新建任务并发起对话）"}
            if data is not None:
                loc = as_dict(data.get("location")).get("name") or ""
                hours = data.get("duration_hours")
                tail = "预计 %s 小时归来" % hours if hours else "预计数小时后归来"
                return {"ok": True, "action": "depart",
                        "msg": "猫猫已出发前往「%s」，%s！" % (loc, tail)}
            return {"ok": False, "msg": "派出旅行失败: %s" % err}
        if mode == "traveling":
            arrive = int(state.get("arrive_at") or 0)
            when = time.strftime("%m-%d %H:%M", time.localtime(arrive)) if arrive else ""
            return {"ok": True, "action": "traveling",
                    "msg": ("猫猫正在旅行途中，预计 %s 归来" % when) if when
                    else "猫猫正在旅行途中，请稍后再来查看！"}
        return {"ok": True, "action": mode, "msg": "当前状态: %s" % (mode or "unknown")}

    @classmethod
    def streak(cls, account, log=None):
        """连登管家：补签保连登 → 礼包 / 补偿 → 档位兑换 → 自动抽奖；全程幂等。"""
        if realm_of(account) != "cn":
            say(log, "连登管家仅限国内版账号")
            return {"ok": False}
        if account.get("enterpriseId") or account.get("apiKey"):
            say(log, "企业版账号无成长体系，跳过")
            return {"ok": True}
        lines = []
        try:
            _s, payload = Activity._chat(account, Activity.HEATMAP, None, 10)
            cells = _data(payload).get("cells") or []
            yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
            cell = next((c for c in cells
                         if isinstance(c, dict) and c.get("date") == yesterday), None)
            if cell and not cell.get("score"):
                _s, payload = Activity._chat(account, Activity.STREAK, None, 10)
                cards = _data(payload, "makeup_cards").get("balance") or 0
                if cards:
                    body = json.dumps({"target_date": yesterday}).encode("utf-8")
                    _s, payload = Activity._chat(account, Activity.MAKEUP_USE, body, 10)
                    if payload.get("code") == 0:
                        lines.append("已消耗 1 张补签卡补签昨日 (%s)" % yesterday)
        except Exception:
            pass
        cfg = REALMS["cn"]
        proxy = str(account.get("proxy") or "")
        for path, label in (("/billing/meter/claim-gift", "新手礼包"),
                            ("/billing/meter/claim-compensation", "活动补偿")):
            try:
                _s, payload = Activity._call(cfg["billing"] + path,
                                             Activity.billing_headers(account), b"{}", proxy, 10)
                if payload.get("code") == 0:
                    credit = _data(payload).get("credit") or 0
                    lines.append("领取%s +%s积分" % (label, credit))
            except Exception:
                pass
        try:
            _s, payload = Activity._chat(account, Activity.STREAK, None, 10)
            data = _data(payload)
            streak = as_dict(data.get("streak"))
            if streak:
                left = streak.get("next_tier_remaining")
                lines.append("连登 %s 天（补签卡 %s 张%s）" % (
                    streak.get("days") or 0,
                    (data.get("makeup_cards") or {}).get("balance") or 0,
                    "，距 %s 档还差 %s 天" % (streak["next_tier"], left)
                    if streak.get("next_tier") and left else ""))
            redemp = data.get("redemption_status") or {}
            statuses = {"7d": redemp.get("tier_7d_status"),
                        "14d": redemp.get("tier_14d_status"),
                        "28d": redemp.get("tier_28d_status")}
            for tier in redemp.get("tiers") or []:
                key = str(tier.get("tier") or "") if isinstance(tier, dict) else ""
                if statuses.get(key) in ("locked", "claimed", None):
                    continue
                body = json.dumps({"tier": key, "client_token": str(uuid.uuid4())}).encode("utf-8")
                _s, res = Activity._chat(account, Activity.REDEEM, body, 10)
                if res.get("code") == 0:
                    lines.append("成功兑换连登 %s 档位" % key)
        except Exception:
            pass
        try:
            drawn = Activity.lottery(account)
            if drawn:
                lines.append("完成成长抽奖 %d 次" % drawn)
        except Exception:
            pass
        for line in lines:
            say(log, line)
        return {"ok": True, "activity": cls.snapshot(account),
                "msg": "; ".join(lines) if lines else "连登状态正常（无需兑换/无抽奖次数）"}

    @staticmethod
    def snapshot(account):
        """积分 + 成长体系一次读齐；失败的部分不进快照。"""
        out = {}
        try:
            out.update(Activity.credits(account))
        except Exception:
            pass
        try:
            out.update(Activity.growth(account))
        except Exception:
            pass
        return out
