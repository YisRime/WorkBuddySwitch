"""不联网的回归测试：账号导入导出、历史统计、定时判断、体检的本地分支。

运行：python -m unittest discover -s tests -v
只用标准库；所有读写都在临时目录里，不碰 data/ 下的真实账号。
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bulk
import probe
import stats
import timer
from store import Store


def token(uid, exp=None, issuer="https://copilot.tencent.com"):
    """造一个结构合法的 JWT（不签名，够解析用）。"""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": uid, "exp": exp or int(time.time()) + 86400, "iss": issuer,
        "name": "测试" + uid[-3:]}).encode()).decode().rstrip("=")
    return "%s.%s.sig" % (header, payload)


class BulkTest(unittest.TestCase):
    """导入解析、导出格式与合并策略。"""

    def setUp(self):
        """每个用例一个干净的临时账号库。"""
        self.tmp = tempfile.mkdtemp(prefix="wb-test-")
        self.store = Store(os.path.join(self.tmp, "Account"))
        os.makedirs(self.store.folder, exist_ok=True)

    def tearDown(self):
        """删掉临时目录。"""
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_shapes(self):
        """JSON 合集包、JSONL、CSV、纯 token 行都能解析出 uid。"""
        rows = [{"uid": "u1", "accessToken": token("u1"), "refreshToken": "r1"},
                {"uid": "u2", "accessToken": token("u2"), "refreshToken": "r2"}]
        pack = json.dumps({"accounts": rows})
        self.assertEqual(sorted(r["uid"] for r in bulk.Bulk.parse(pack)[0]), ["u1", "u2"])

        jsonl = "\n".join(json.dumps({"account": {"uid": item["uid"]},
                                      "auth": {"accessToken": item["accessToken"]}})
                          for item in rows)
        got = bulk.Bulk.parse(jsonl)[0]
        self.assertEqual(sorted(r["uid"] for r in got), ["u1", "u2"])

        csv_text = "u3,%s,%s" % (token("u3"), "r3")
        self.assertEqual(bulk.Bulk.parse(csv_text)[0][0]["uid"], "u3")

        single = bulk.Bulk.parse(token("u4"))[0][0]
        self.assertEqual(single["uid"], "u4")
        self.assertEqual(single["realm"], "cn")

    def test_norm_fills_fields(self):
        """规范一条记录：realm / domain / expiresAt 都能从 token 补出来。"""
        row = bulk.Bulk.norm({"uid": "u9", "accessToken": token("u9")})
        self.assertEqual(row["realm"], "cn")
        self.assertEqual(row["domain"], "copilot.tencent.com")
        self.assertGreater(row["expiresAt"], time.time())
        self.assertEqual(bulk.Bulk.missing(row), ["refreshToken"])

    def test_export_roundtrip(self):
        """五种导出格式都能再导回来，uid 不变。"""
        rows = [{"uid": "u1", "nickname": "甲", "accessToken": token("u1"),
                 "refreshToken": "r1", "realm": "cn", "domain": "copilot.tencent.com"},
                {"uid": "u2", "nickname": "乙", "accessToken": token("u2", issuer="https://www.workbuddy.ai"),
                 "refreshToken": "r2", "realm": "intl", "domain": "www.workbuddy.ai"}]
        for fmt in ("json", "jsonl", "csv", "txt", "zip"):
            path = os.path.join(self.tmp, "out." + fmt)
            bulk.Bulk.write(path, rows)
            got = bulk.Bulk.read(path)[0]
            self.assertEqual(sorted(row["uid"] for row in got), ["u1", "u2"], fmt)

    def test_single_info_export(self):
        """单账号 .info 导出后能被自己的解析器读回 uid。"""
        row = {"uid": "u5", "nickname": "丙", "accessToken": token("u5"),
               "refreshToken": "r5", "domain": "copilot.tencent.com"}
        path = os.path.join(self.tmp, "one.info")
        bulk.Bulk.write(path, [row])
        self.assertEqual(bulk.Bulk.read(path)[0][0]["uid"], "u5")

    def test_merge_and_dedupe(self):
        """同 uid 合并而不是新增；凭证更旧时不覆盖。"""
        fresh = {"uid": "u1", "nickname": "甲", "accessToken": token("u1"),
                 "refreshToken": "r1"}
        self.assertEqual(bulk.Bulk.merge(self.store, [fresh])["count"], 1)
        again = dict(fresh, nickname="甲改")
        result = bulk.Bulk.merge(self.store, [again])
        self.assertEqual((len(result["updated"]), len(result["added"])), (1, 0))
        stale = {"uid": "u1", "accessToken": token("u1", exp=int(time.time()) + 10),
                 "refreshToken": "old"}
        result = bulk.Bulk.merge(self.store, [stale])
        self.assertEqual(len(result["skipped"]), 1)

    def test_mask(self):
        """脱敏：token 留头尾，昵称与 uid 打码。"""
        masked = bulk.Bulk.mask({"uid": "1234567890", "nickname": "小明明",
                                 "accessToken": "a" * 30})
        self.assertNotIn("a" * 30, masked["accessToken"])
        self.assertTrue(masked["nickname"].startswith("小"))
        self.assertIn("*", masked["uid"])

    def test_clean(self):
        """坏文件与重复文件被挪进回收目录，好文件留下。"""
        os.makedirs(self.store.folder, exist_ok=True)
        good = os.path.join(self.store.folder, "good.json")
        with open(good, "w", encoding="utf-8") as fh:
            json.dump({"uid": "u1", "accessToken": token("u1")}, fh)
        with open(os.path.join(self.store.folder, "bad.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with open(os.path.join(self.store.folder, "dup.json"), "w", encoding="utf-8") as fh:
            json.dump({"uid": "u1", "accessToken": token("u1", exp=int(time.time()) - 10)}, fh)
        result = bulk.Bulk.clean(self.store)
        self.assertEqual(result["moved"], 2)
        self.assertTrue(os.path.isfile(good))


class StatsTest(unittest.TestCase):
    """历史快照的写入与汇总。"""

    def setUp(self):
        """临时 history 文件。"""
        self.tmp = tempfile.mkdtemp(prefix="wb-stats-")
        self.path = os.path.join(self.tmp, "history.jsonl")

    def tearDown(self):
        """清理。"""
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_and_trend(self):
        """写两天快照后能算出每天的变化。"""
        stats.Stats.record("u1", {"remain": 900, "size": 1000}, path=self.path)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": int(time.time()) - 86400, "uid": "u1",
                                 "remain": 1000, "size": 1000}) + "\n")
        points = stats.Stats.daily("u1", 7, path=self.path)
        self.assertEqual(len(points), 2)
        trend = stats.Stats.trend("u1", 7, path=self.path)
        self.assertEqual(trend["delta"], -100)
        self.assertEqual(trend["perDay"], -100)


class TimerTest(unittest.TestCase):
    """定时判断：到点、去重、补跑。"""

    def setUp(self):
        """把设置文件指到临时目录。"""
        self.tmp = tempfile.mkdtemp(prefix="wb-timer-")
        self.saved = timer.SETTINGS
        timer.SETTINGS = os.path.join(self.tmp, "settings.json")

    def tearDown(self):
        """还原设置路径。"""
        timer.SETTINGS = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _clock(hour, minute):
        """造一个指定时分的时间结构。"""
        return time.localtime(time.mktime((2026, 11, 21, hour, minute, 0, 0, 0, -1)))

    def test_due(self):
        """没过点不跑；过点跑一次；跑过就记进 last。"""
        timer.Timer.configure(enabled=True, time="09:00", actions=["checkin"],
                              catchup=True, last={})
        scheduler = timer.Timer({"checkin": lambda entry, log: None}, lambda: [])
        self.assertEqual(scheduler.due(now=self._clock(8, 59)), [])
        self.assertEqual(scheduler.due(now=self._clock(9, 1)), ["checkin"])
        timer.Timer.configure(last={time.strftime("%Y-%m-%d"): ["checkin"]})
        self.assertEqual(scheduler.due(now=self._clock(9, 2)), [])

    def test_disabled_and_catchup(self):
        """关掉就不跑；补跑关掉且已过点时也不跑。"""
        timer.Timer.configure(enabled=False, time="09:00", actions=["checkin"], last={})
        scheduler = timer.Timer({"checkin": lambda entry, log: None}, lambda: [])
        self.assertEqual(scheduler.due(now=self._clock(10, 0)), [])
        timer.Timer.configure(enabled=True, catchup=False, last={})
        self.assertEqual(scheduler.due(now=self._clock(10, 0)), [])


class ProbeTest(unittest.TestCase):
    """本地体检分支（不联网）。"""

    def test_local_checks(self):
        """缺 refreshToken 与已过期都能报出来。"""
        tmp = tempfile.mkdtemp(prefix="wb-probe-")
        try:
            store = Store(tmp)
            path = store.upsert({"uid": "u1", "nickname": "甲",
                                 "accessToken": token("u1", exp=int(time.time()) - 10),
                                 "refreshToken": ""})
            entry = store.load()[0][0]
            levels = [level for level, _text in probe.Probe.local(entry)]
            self.assertIn(probe.EXPIRED, levels)
            self.assertIn(probe.WARN, levels)
            self.assertTrue(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SignupTest(unittest.TestCase):
    """注册链路里不联网的那一段：上游 token 载荷 → 账号库的一行。"""

    def test_row_build(self):
        """token 载荷 + 账号信息能拼成完整记录，uid 与到期都补齐。"""
        import signup
        access = token("uid-777")
        row = signup.Signup._row(
            {"accessToken": access, "refreshToken": "refresh-1", "expiresIn": 3600,
             "refreshExpiresIn": 7200, "domain": "copilot.tencent.com"},
            {"uid": "uid-777", "nickname": "小明", "phoneNumber": "13800000000"})
        self.assertEqual(row["uid"], "uid-777")
        self.assertEqual(row["nickname"], "小明")
        self.assertEqual(row["realm"], "cn")
        self.assertEqual(row["phone"], "13800000000")
        self.assertGreater(row["expiresAt"], time.time())

    def test_mobile_normalize(self):
        """手机号规范化：去掉 +86 与分隔符，非法号码报错。"""
        import signup
        self.assertEqual(signup.Signup.mobile("+86 138-0000-0000"),
                         ("13800000000", "86"))
        with self.assertRaises(Exception):
            signup.Signup.mobile("12345")


if __name__ == "__main__":
    unittest.main()
