"""本地会话用量统计：把客户端自己记的 token / 积分用量汇总成一份报表。

数据源（有哪个算哪个，全部只读、不联网）：

* 编辑器 / 扩展历史：<本地数据根>/CodeBuddyExtension/Data/<uid>/<client>/<uid>/history/
  下的 JSON，requests[].usage 里有 inputTokens / outputTokens / cacheTokens /
  cachedWriteTokens / credit 这些字段；
* WorkBuddy AI 会话：~/.workbuddy-ai/projects/*.jsonl（一行一个消息，usage 在 message 里）；
* CLI 会话：~/.codebuddy/projects/*.jsonl；
* WorkBuddy 会话：~/.workbuddy/projects/*.jsonl。

做法：递归扫每个 JSON / JSONL，凡是长得像 usage 的字典（带 input / output token 字段）
就按「最近的 model 与时间戳」归一天；带 id 的按 id 去重——同一个请求在索引和消息里
各存一份时不会重复计。
"""
import json
import os
import sys
import time

from util import DATA, as_dict, norm_ts

INPUT_KEYS = ("inputTokens", "input_tokens", "prompt_tokens", "promptTokens")
OUTPUT_KEYS = ("outputTokens", "output_tokens", "completion_tokens", "completionTokens")
CACHE_READ_KEYS = ("cacheTokens", "cacheReadInputTokens", "cache_read_input_tokens")
CACHE_WRITE_KEYS = ("cachedWriteTokens", "cacheWriteTokens", "prompt_cache_write_tokens")
CREDIT_KEYS = ("credit", "credits", "cost")
MODEL_KEYS = ("model", "modelId", "requestModelId", "modelName")
TIME_KEYS = ("timestamp", "startedAt", "createdAt", "created_at", "time", "at")


def _num(node, keys):
    """按候选键名取第一个能转成数字的值；没有给 None。"""
    for key in keys:
        if key in node and node[key] not in (None, ""):
            try:
                return int(float(node[key]))
            except (TypeError, ValueError):
                continue
    return None


def _local_root():
    """本地数据根：Windows 是 %LOCALAPPDATA%，其它平台按各自惯例。"""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
        return local
    home = os.path.expanduser("~")
    return (os.path.join(home, "Library", "Application Support") if sys.platform == "darwin"
            else os.path.join(home, ".local", "share"))


class Usage(object):
    """一次扫描的结果与报表输出。"""

    @classmethod
    def sources(cls):
        """本机存在的数据源：[(显示名, 根目录, 是否递归)]。"""
        out = []
        data_root = os.path.join(_local_root(), "CodeBuddyExtension", "Data")
        if os.path.isdir(data_root):
            out.append(("编辑器历史", data_root, True))
        home = os.path.expanduser("~")
        for name, path in (("WorkBuddy AI 会话", os.path.join(home, ".workbuddy-ai", "projects")),
                           ("CLI 会话", os.path.join(home, ".codebuddy", "projects")),
                           ("WorkBuddy 会话", os.path.join(home, ".workbuddy", "projects"))):
            if os.path.isdir(path):
                out.append((name, path, True))
        return out

    # ---------- 解析 ----------

    @staticmethod
    def read_usage(node):
        """这个字典像不像一条 usage；像就返回标准化后的记录。"""
        if not isinstance(node, dict):
            return None
        plain = _num(node, INPUT_KEYS)
        output = _num(node, OUTPUT_KEYS)
        if plain is None and output is None:
            return None
        return {
            "input": plain or 0,
            "output": output or 0,
            "cache_read": _num(node, CACHE_READ_KEYS) or 0,
            "cache_write": _num(node, CACHE_WRITE_KEYS) or 0,
            "credit": _num(node, CREDIT_KEYS) or 0,
            "total": _num(node, ("totalTokens", "total_tokens")) or ((plain or 0) + (output or 0)),
        }

    @classmethod
    def _walk(cls, node, ctx, sink):
        """递归找 usage；找到就记一条并跳过它自己的子树（避免重复）。"""
        if isinstance(node, dict):
            model = str(node.get("model") or node.get("modelId")
                        or node.get("requestModelId") or ctx["model"] or "")
            stamp = ctx["stamp"]
            for key in TIME_KEYS:
                if node.get(key) not in (None, ""):
                    stamp = norm_ts(node[key])
                    break
            usage = cls.read_usage(node)
            if usage is not None:
                ident = str(node.get("id") or node.get("requestId") or "")
                sink.add(usage, model, stamp, ident)
                return
            for value in node.values():
                cls._walk(value, {"model": model, "stamp": stamp}, sink)
        elif isinstance(node, list):
            for item in node:
                cls._walk(item, ctx, sink)

    # ---------- 扫描 ----------

    @classmethod
    def scan(cls, days=30, roots=None, log=None):
        """扫本机会话，返回总计 / 按天 / 按模型 / 按来源。"""
        floor = int(time.time()) - max(1, days) * 86400
        total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                 "credit": 0, "total": 0}
        by_day, by_model, per_source = {}, {}, []
        seen = set()
        for name, root, recursive in (roots or cls.sources()):
            files, used, counts = 0, 0, {"input": 0, "output": 0, "cache_read": 0,
                                         "cache_write": 0, "credit": 0, "total": 0}

            def sink_add(usage, model, stamp, ident):
                """一条 usage 的落点：去重、判窗口、累加。"""
                nonlocal used
                key = ident or "%s|%s|%s|%s" % (stamp, usage["input"], usage["output"],
                                                usage["total"])
                if key in seen:
                    return
                seen.add(key)
                if stamp and stamp < floor:
                    return
                used += 1
                for field in counts:
                    counts[field] += usage[field]
                day = (time.strftime("%Y-%m-%d", time.localtime(stamp)) if stamp else "未知")
                bucket = by_day.setdefault(day, {"input": 0, "output": 0, "credit": 0,
                                                 "total": 0})
                for field in bucket:
                    bucket[field] += usage.get(field, 0)
                label = model or "未知"
                model_bucket = by_model.setdefault(label, {"input": 0, "output": 0,
                                                           "credit": 0, "total": 0})
                for field in model_bucket:
                    model_bucket[field] += usage.get(field, 0)

            class _Sink(object):
                """把 sink_add 收成一个带 add 方法的对象。"""

                def add(self, usage, model, stamp, ident):
                    """转发到外层闭包。"""
                    sink_add(usage, model, stamp, ident)

            sink = _Sink()
            for path in cls._files(root, recursive):
                files += 1
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                if path.lower().endswith(".jsonl"):
                    for line in text.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            cls._walk(json.loads(line), {"model": "", "stamp": 0}, sink)
                        except ValueError:
                            continue
                else:
                    try:
                        cls._walk(json.loads(text), {"model": "", "stamp": 0}, sink)
                    except ValueError:
                        continue
            for field in total:
                total[field] += counts[field]
            per_source.append({"name": name, "root": root, "files": files,
                               "requests": used, "counts": counts})
            if log:
                log("%s：%d 个文件、%d 条请求、%s tokens" % (
                    name, files, used, counts["total"]))
        return {"generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "days": days,
                "totals": total, "byDay": by_day, "byModel": by_model,
                "sources": per_source}

    @staticmethod
    def _files(root, recursive):
        """列 JSON / JSONL 文件；递归时跳过明显的二进制目录。"""
        out = []
        if not recursive:
            try:
                names = os.listdir(root)
            except OSError:
                return out
            for name in names:
                if name.lower().endswith((".json", ".jsonl")):
                    out.append(os.path.join(root, name))
            return out
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [name for name in dirnames
                           if name not in ("node_modules", "logs", "Public", "backups")]
            for name in files:
                if name.lower().endswith((".json", ".jsonl")):
                    out.append(os.path.join(dirpath, name))
        return out

    # ---------- 报表 ----------

    @classmethod
    def report(cls, days=30, folder=None, roots=None):
        """生成 Markdown 报表，返回 (路径, 扫描结果)。"""
        result = cls.scan(days, roots)
        totals = result["totals"]
        lines = ["# WorkBuddy Switch 用量报表", "",
                 "生成时间：%s · 窗口：最近 %d 天" % (result["generatedAt"], days), "",
                 "| 指标 | 数值 |", "|---|---|",
                 "| 输入 tokens | %s |" % _fmt(totals["input"]),
                 "| 输出 tokens | %s |" % _fmt(totals["output"]),
                 "| 缓存读 tokens | %s |" % _fmt(totals["cache_read"]),
                 "| 缓存写 tokens | %s |" % _fmt(totals["cache_write"]),
                 "| 合计 tokens | %s |" % _fmt(totals["total"]),
                 "| 消耗积分 | %s |" % _fmt(totals["credit"]),
                 "", "## 按天", "", "| 日期 | 输入 | 输出 | 合计 tokens | 积分 |",
                 "|---|---|---|---|---|"]
        for day in sorted(result["byDay"], reverse=True)[:14]:
            row = result["byDay"][day]
            lines.append("| %s | %s | %s | %s | %s |" % (day, _fmt(row["input"]),
                                                        _fmt(row["output"]),
                                                        _fmt(row["total"]),
                                                        _fmt(row["credit"])))
        if result["byModel"]:
            lines += ["", "## 按模型", "", "| 模型 | 输入 | 输出 | 合计 | 积分 |",
                      "|---|---|---|---|---|"]
            for model, row in sorted(result["byModel"].items(),
                                     key=lambda item: -item[1]["total"])[:12]:
                lines.append("| %s | %s | %s | %s | %s |" % (model, _fmt(row["input"]),
                                                            _fmt(row["output"]),
                                                            _fmt(row["total"]),
                                                            _fmt(row["credit"])))
        lines += ["", "## 数据源", "", "| 来源 | 文件 | 请求 | 合计 tokens |",
                  "|---|---|---|---|"]
        for row in result["sources"]:
            lines.append("| %s | %d | %d | %s |" % (row["name"], row["files"],
                                                   row["requests"],
                                                   _fmt(row["counts"]["total"])))
        lines += ["", "说明：只在本地读客户端自己记的会话文件，不上传、不联网。"]
        folder = folder or os.path.join(DATA, "Reports")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "usage-%s.md" % time.strftime("%Y%m%d-%H%M%S"))
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        return path, result


def _fmt(number):
    """大数字折成「万」，便于一眼看清。"""
    try:
        value = float(number)
    except (TypeError, ValueError):
        return "—"
    if abs(value) >= 10000:
        return "%.1f万" % (value / 10000.0)
    return "%d" % value
