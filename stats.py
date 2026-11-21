"""积分历史与用量统计：把每次「获取 / 签到」的结果追加成一行一条的历史。

data/Stats/history.jsonl 一行一条快照（at / uid / remain / size / used / 包数 /
签到态）。读的时候按 uid + 日期取当天最后一条——文件因此可以一直追加，天然是一份
审计流水；写的时候只追加，不回头改。
"""
import json
import os
import time

from util import DATA, fmt_num, norm_ts, read_json

HISTORY = os.path.join(DATA, "Stats", "history.jsonl")


def _stamp(value):
    """统一成秒级时间戳。"""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return int(number / 1000.0) if number > 10 ** 11 else int(number)


class Stats(object):
    """历史快照的读写与汇总。"""

    @staticmethod
    def record(uid, info, path=None):
        """追加一条快照（字段缺就留空）；info 里没有额度信息就不记。"""
        info = info or {}
        if info.get("remain") is None and info.get("size") is None:
            return False
        row = {"at": int(time.time()), "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
               "uid": str(uid or ""), "remain": info.get("remain"), "size": info.get("size"),
               "used": info.get("used"), "packages": info.get("packages"),
               "checkin": info.get("checkin") or "", "daily": bool(info.get("daily"))}
        path = path or HISTORY
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False

    @staticmethod
    def load(path=None, days=90):
        """读最近 days 天的全部快照；坏行直接跳过。"""
        path = path or HISTORY
        if not os.path.isfile(path):
            return []
        floor = time.time() - max(1, days) * 86400
        rows = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and _stamp(row.get("at")) >= floor:
                        row["at"] = _stamp(row.get("at"))
                        rows.append(row)
        except OSError:
            return []
        return sorted(rows, key=lambda item: item["at"])

    @classmethod
    def daily(cls, uid=None, days=14, path=None):
        """按天归并（同一天取最后一条），返回 [{date, remain, size}]。"""
        rows = cls.load(path, days)
        picked = {}
        for row in rows:
            if uid and str(row.get("uid")) != str(uid):
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(row["at"]))
            picked[day] = row
        return [{"date": day, "remain": picked[day].get("remain"),
                 "size": picked[day].get("size")}
                for day in sorted(picked)]

    @classmethod
    def trend(cls, uid, days=7, path=None):
        """最近两天的额度差，用来提示「每天大概掉多少」。"""
        points = cls.daily(uid, days, path)
        if len(points) < 2:
            return {"delta": None, "perDay": None, "points": len(points)}
        first, last = points[0], points[-1]
        try:
            delta = float(last["remain"]) - float(first["remain"])
        except (TypeError, ValueError):
            return {"delta": None, "perDay": None, "points": len(points)}
        span = max(1.0, (_stamp(last["date"] + " 00:00:00")
                         - _stamp(first["date"] + " 00:00:00")) / 86400.0)
        return {"delta": round(delta, 2), "perDay": round(delta / span, 2),
                "points": len(points)}

    @classmethod
    def report(cls, entries, days=7, path=None):
        """生成一份 Markdown 报表，返回 (文本, 每号趋势)。"""
        lines = ["# WorkBuddy Switch 统计", "",
                 "生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"), "",
                 "| 账号 | 区域 | 剩余积分 | 总量 | 今日变化 | 近 %d 天 |" % days,
                 "|---|---|---|---|---|---|"]
        trends = {}
        for entry in entries:
            info = cls.trend(entry.uid, days, path)
            trends[entry.uid] = info
            credits = entry.credits or {}
            remain = credits.get("remain")
            size = credits.get("size")
            delta = info.get("delta")
            lines.append("| %s | %s | %s | %s | %s | %s |" % (
                entry.nickname, "国内版" if entry.realm == "cn" else "国际版",
                fmt_num(remain) if remain is not None else "—",
                fmt_num(size) if size is not None else "—",
                ("%+.1f" % delta) if isinstance(delta, (int, float)) else "—",
                ("%+.1f/天" % info["perDay"]) if isinstance(info.get("perDay"), (int, float))
                else "—"))
        total = sum(float((entry.credits or {}).get("remain") or 0) for entry in entries)
        lines += ["", "合计剩余：%s" % fmt_num(total), "",
                  "数据来源：data/Stats/history.jsonl（%d 条）" % len(cls.load(path, days))]
        return "\n".join(lines), trends

    @staticmethod
    def save_report(entries, days=7, folder=None):
        """把报表写到 data/Reports/，返回文件路径。"""
        folder = folder or os.path.join(DATA, "Reports")
        os.makedirs(folder, exist_ok=True)
        text, _trends = Stats.report(entries, days)
        path = os.path.join(folder, "stats-%s.md" % time.strftime("%Y%m%d-%H%M%S"))
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        return path
