"""本机定时任务：到点自动签到 / 上报活跃 / 做任务 / 旅行 / 连登。

配置写在 data/settings.json 的 schedule 段：

    {"enabled": true, "time": "08:05", "actions": ["checkin", "report"],
     "catchup": true, "last": {"2026-11-21": ["checkin", "report"]}}

调度线程每 20 秒醒一次，只做「今天该做、还没做过」的动作：catchup 打开时错过
时点当天也补跑（上班开机才有空的那种用法），关掉就严格等到第二天。所有动作都在
后台线程里跑，结果只经 log 回调出去，不碰界面控件。
"""
import json
import os
import threading
import time

from util import DATA, as_dict, read_json, write_json

SETTINGS = os.path.join(DATA, "settings.json")
DEFAULTS = {
    "schedule": {"enabled": False, "time": "09:00",
                 "actions": ["checkin", "report"], "catchup": True, "last": {}},
}
ACTIONS = ("checkin", "report", "tasks", "travel", "streak")
LABELS = {"checkin": "签到", "report": "活跃", "tasks": "任务",
          "travel": "旅行", "streak": "管家"}


class Settings(object):
    """data/settings.json 的读写（账号目录之外的全局设置都放这里）。"""

    _lock = threading.Lock()

    @staticmethod
    def load():
        """读设置；文件缺失或坏掉就给默认值。"""
        try:
            doc = read_json(SETTINGS)
            if not isinstance(doc, dict):
                raise ValueError("不是对象")
        except Exception:
            doc = {}
        merged = json.loads(json.dumps(DEFAULTS))
        for key, value in doc.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def save(doc):
        """整份写回（原子写）。"""
        with Settings._lock:
            write_json(SETTINGS, doc)
        return doc

    @staticmethod
    def schedule():
        """只取定时任务那一段。"""
        return as_dict(Settings.load().get("schedule"))

    @staticmethod
    def configure(patch):
        """合并更新 schedule 段并落盘，返回新的 schedule。"""
        doc = Settings.load()
        section = as_dict(doc.get("schedule"))
        section.update(patch or {})
        doc["schedule"] = section
        Settings.save(doc)
        return section


class Timer(object):
    """后台调度：按天去重地触发已注册的动作。"""

    TICK = 20.0

    def __init__(self, actions, targets, log=None):
        """actions: {名称: fn(entry, log)}；targets: () -> [Entry]；log: 写一行日志。"""
        self.actions = dict(actions or {})
        self.targets = targets
        self.log = log or (lambda _message: None)
        self.stop_flag = threading.Event()
        self.thread = None
        self.running = False

    # ---------- 配置 ----------

    @staticmethod
    def config():
        """当前定时配置。"""
        return Settings.schedule()

    @staticmethod
    def configure(**patch):
        """改定时配置（enabled / time / actions / catchup）。"""
        return Settings.configure(patch)

    @staticmethod
    def _today():
        """今天的日期串。"""
        return time.strftime("%Y-%m-%d")

    # ---------- 到点判断 ----------

    def due(self, now=None):
        """今天还没做、且已到点的动作列表。"""
        section = self.config()
        if not section.get("enabled"):
            return []
        clock = str(section.get("time") or "09:00")
        try:
            hour, minute = (int(part) for part in clock.split(":")[:2])
        except (TypeError, ValueError):
            hour, minute = 8, 5
        now = now or time.localtime()
        done = list((as_dict(section.get("last")).get(self._today()) or []))
        chosen = [name for name in (section.get("actions") or []) if name in self.actions]
        after = (now.tm_hour, now.tm_min) >= (hour, minute)
        if not after:
            return []
        if not section.get("catchup") and (now.tm_hour, now.tm_min) > (hour, minute + 30):
            return []
        return [name for name in chosen if name not in done]

    def run_due(self, now=None):
        """跑一遍到点的动作；每个动作对全部目标账号执行，返回汇总。"""
        due = self.due(now)
        if not due:
            return {"ran": [], "count": 0}
        rows = []
        for name in due:
            func = self.actions.get(name)
            if func is None:
                continue
            targets = list(self.targets() or [])
            ok, bad = 0, []
            for entry in targets:
                try:
                    func(entry, self.log)
                    ok += 1
                except Exception as exc:
                    bad.append("%s：%s" % (entry.nickname, exc))
            rows.append({"action": name, "ok": ok, "bad": bad})
            self.log("定时%s：成功 %d / %d%s" % (LABELS.get(name, name), ok, len(targets),
                                              ("，失败：" + "；".join(bad[:3])) if bad else ""))
        section = self.config()
        last = as_dict(section.get("last"))
        for item in due:
            bucket = list(last.get(self._today()) or [])
            if item not in bucket:
                bucket.append(item)
            last[self._today()] = bucket
        # 只留最近 14 天，别让设置文件无限长
        for day in sorted(last)[:-14]:
            last.pop(day, None)
        Settings.configure(last=last)
        return {"ran": due, "count": len(rows), "rows": rows}

    # ---------- 线程 ----------

    def start(self):
        """起调度线程（已经在跑就不重复起）。"""
        if self.thread and self.thread.is_alive():
            return False
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.running = True
        return True

    def stop(self):
        """停调度线程。"""
        self.stop_flag.set()
        self.running = False

    def _loop(self):
        """每 TICK 秒看一眼：配置开着才真的跑。"""
        while not self.stop_flag.is_set():
            try:
                if self.config().get("enabled"):
                    self.run_due()
            except Exception as exc:
                self.log("定时任务异常：%s" % exc)
            self.stop_flag.wait(self.TICK)
