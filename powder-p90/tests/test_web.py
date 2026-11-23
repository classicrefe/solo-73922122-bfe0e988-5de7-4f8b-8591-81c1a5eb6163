"""Flask 网页端到端测试：上传、相容/不相容补报、勾选核算、见证展示、并发上传。"""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app.db as app_db  # noqa: E402
from app.web import create_app  # noqa: E402

ALPHA_T1 = "0 99 400\n100 199 500\n"
ALPHA_T2 = "0 49 100\n50 99 300\n100 149 400\n150 199 100\n"
ALPHA_BAD = "0 49 450\n50 99 0\n100 199 450\n"
BETA_T1 = "0 100 500\n101 200 500\n"
GAMMA_T1 = "0 9 20\n10 19 30\n20 29 50\n"

# 两张补报各自与 OLD_T 相容，但彼此冲突（[0,4] 一报 50 粒、一报 90 粒）
OLD_T = "0 9 100\n10 19 100\n"
SUP_A = "0 4 50\n5 9 50\n10 19 100\n"
SUP_B = "0 4 90\n5 9 10\n10 19 100\n"


class WebFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)
        self.app = create_app(db_path=self.tmp.name)
        self.client = self.app.test_client()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _upload(self, name, text, note=""):
        return self.client.post(
            "/upload",
            data={"batch_name": name, "table_text": text, "note": note},
            follow_redirects=True,
        )

    def test_full_flow(self):
        # 首页可打开
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("粒径档表 P90", html)

        # 三个批次首次上传
        for name, text in [("B-ALPHA", ALPHA_T1), ("B-BETA", BETA_T1), ("B-GAMMA", GAMMA_T1)]:
            rv = self._upload(name, text)
            self.assertEqual(rv.status_code, 200)
            self.assertIn("入库", rv.get_data(as_text=True))

        # 相容补报成功
        rv = self._upload("B-ALPHA", ALPHA_T2, note="细分补报")
        body = rv.get_data(as_text=True)
        self.assertIn("补报成功", body)
        self.assertIn("现共 2 张档表", body)

        # 不相容补报被拒绝，且档表数不增加
        rv = self._upload("B-ALPHA", ALPHA_BAD)
        body = rv.get_data(as_text=True)
        self.assertIn("补报被拒绝", body)
        self.assertIn("未入库", body)

        # 批次详情仍只有 2 张档表
        with self.app.app_context():
            from app.db import connect, list_tables
            with connect(self.tmp.name) as conn:
                from app.db import get_batch_by_name
                bid = int(get_batch_by_name(conn, "B-ALPHA")["id"])
                self.assertEqual(len(list_tables(conn, bid)), 2)
        detail = self.client.get(f"/batch/{bid}").get_data(as_text=True)
        self.assertIn("档表 #", detail)
        self.assertEqual(detail.count("900 粒"), 2)  # 两张已入库档表，总粒数均为 900
        self.assertIn("[0, 49]", detail)  # 第二张细分档表确实在库

        # 解析失败不入库
        rv = self._upload("B-BAD", "0 9 5\n11 19 3\n")
        self.assertIn("解析失败", rv.get_data(as_text=True))

        # 勾选三批核算
        rv = self.client.post("/compute", data={"batch_ids": [1, 2, 3]}, follow_redirects=True)
        self.assertEqual(rv.status_code, 200)
        body = rv.get_data(as_text=True)
        self.assertIn("合并 P90 核算结果", body)
        # 总粒数 900+1000+100 = 2000，k=1800
        self.assertIn("2000", body)
        self.assertIn("1800", body)
        self.assertIn("下界见证", body)
        self.assertIn("上界见证", body)
        self.assertIn("B-ALPHA", body)
        # 端点以“微米”展示
        self.assertRegex(body, r"最小可能值.*微米")

    def test_compute_requires_selection(self):
        self._upload("B-ALPHA", ALPHA_T1)
        rv = self.client.post("/compute", data={}, follow_redirects=True)
        self.assertIn("请至少勾选一个批次", rv.get_data(as_text=True))

    def test_invalid_upload_messages(self):
        rv = self._upload("X", "0 9 0\n10 19 0\n")
        self.assertIn("整表粒数为 0", rv.get_data(as_text=True))
        rv = self.client.post(
            "/upload", data={"batch_name": "", "table_text": "0 9 1"}, follow_redirects=True
        )
        self.assertIn("批号不能为空", rv.get_data(as_text=True))


def _run_concurrently(app, posts):
    """并发跑一组 POST /upload，返回 {tag: (status_code, body)}。"""
    barrier = threading.Barrier(len(posts))
    results = {}

    def worker(tag, data):
        client = app.test_client()
        barrier.wait()
        rv = client.post("/upload", data=data, follow_redirects=True)
        results[tag] = (rv.status_code, rv.get_data(as_text=True))

    threads = [
        threading.Thread(target=worker, args=(tag, data)) for tag, data in posts
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def _slow(module, name, seconds=0.4):
    """把 module.name 包一层 sleep，返回还原函数。"""
    original = getattr(module, name)

    def wrapper(*args, **kwargs):
        time.sleep(seconds)
        return original(*args, **kwargs)

    setattr(module, name, wrapper)
    return lambda: setattr(module, name, original)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)
        self.app = create_app(db_path=self.tmp.name)
        self.client = self.app.test_client()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _tables(self, name):
        from app.db import connect, get_batch_by_name, list_tables

        with connect(self.tmp.name) as conn:
            row = get_batch_by_name(conn, name)
            if row is None:
                return 0, None
            return len(list_tables(conn, int(row["id"]))), int(row["id"])

    def _upload_ok(self, name, text, note=""):
        rv = self.client.post(
            "/upload",
            data={"batch_name": name, "table_text": text, "note": note},
            follow_redirects=True,
        )
        self.assertEqual(rv.status_code, 200)
        return rv

    def test_concurrent_first_uploads_create_one_batch(self):
        """两人同时给同一新批号首报：不得有 500，只建一个批次、两表都归入该批。"""
        # 放大建批竞态窗口，旧实现下这里必现 UNIQUE constraint failed -> 500
        restore = _slow(app_db, "get_batch_by_name", 0.3)
        try:
            results = _run_concurrently(
                self.app,
                [
                    ("A", {"batch_name": "RACE", "table_text": ALPHA_T1, "note": "A"}),
                    ("B", {"batch_name": "RACE", "table_text": ALPHA_T2, "note": "B"}),
                ],
            )
        finally:
            restore()

        for tag, (status, body) in results.items():
            self.assertEqual(status, 200, f"{tag} 收到 HTTP {status}")
            self.assertNotIn("系统繁忙", body)
            self.assertNotIn("补报被拒绝", body)
        # 一张建批、一张走补报，两者都已入库
        self.assertEqual(
            sum("已创建" in body for _, body in results.values()), 1,
            f"应只创建一次批次：{results}",
        )
        self.assertEqual(
            sum("补报成功" in body for _, body in results.values()), 1,
            f"另一张应按补报归入同一批：{results}",
        )

        from app.db import connect, get_batch_by_name, list_batches

        with connect(self.tmp.name) as conn:
            self.assertEqual(len(list_batches(conn)), 1, "同一新批号应只建一个批次")
            row = get_batch_by_name(conn, "RACE")
            self.assertIsNotNone(row)
            self.assertEqual(len(app_db.list_tables(conn, int(row["id"]))), 2)

    def test_concurrent_conflicting_supplements_only_one_wins(self):
        """两张补报各自与旧表相容、彼此冲突：只收先入库的一张，另一张提示冲突。"""
        self._upload_ok("B", OLD_T)
        restore = _slow(app_db, "load_batch_tables", 0.4)
        try:
            results = _run_concurrently(
                self.app,
                [
                    ("A", {"batch_name": "B", "table_text": SUP_A, "note": "补 A"}),
                    ("B", {"batch_name": "B", "table_text": SUP_B, "note": "补 B"}),
                ],
            )
        finally:
            restore()

        statuses = {tag: st for tag, (st, _) in results.items()}
        bodies = {tag: body for tag, (_, body) in results.items()}
        self.assertEqual(statuses, {"A": 200, "B": 200})
        accepted = [tag for tag in bodies if "补报成功" in bodies[tag]]
        rejected = [tag for tag in bodies if "补报被拒绝" in bodies[tag]]
        self.assertEqual(len(accepted), 1, f"应恰有一张入库，实际 {bodies}")
        self.assertEqual(len(rejected), 1, f"应恰有一张被退回，实际 {bodies}")
        self.assertIn("不相容", bodies[rejected[0]])
        self.assertIn("冲突", bodies[rejected[0]])
        self.assertIn("核算仍按原档表进行", bodies[rejected[0]])

        count, bid = self._tables("B")
        self.assertEqual(count, 2, "库里应只保留旧表 + 先入库的那张补报")

        # 已入库档表仍可用于核算
        rv = self.client.post("/compute", data={"batch_ids": [bid]}, follow_redirects=True)
        body = rv.get_data(as_text=True)
        self.assertEqual(rv.status_code, 200)
        self.assertIn("合并 P90 核算结果", body)
        self.assertNotIn("核算失败", body)

    def test_supplement_to_other_batch_not_blocked(self):
        """某批的相容性校验尚未完成时，其他批次的补报仍可入库。"""
        self._upload_ok("SLOW", OLD_T)
        self._upload_ok("FAST", OLD_T)
        # 让 SLOW 批在持锁校验中途卡住（首次 load_batch_tables 等待事件）
        real_load = app_db.load_batch_tables
        check_started = threading.Event()
        release = threading.Event()

        def gated_load(conn, batch_id):
            loadable = real_load(conn, batch_id)
            if loadable and not check_started.is_set():
                check_started.set()
                release.wait(timeout=5)
            return loadable

        app_db.load_batch_tables = gated_load
        slow_result = {}

        def slow_post():
            c = self.app.test_client()
            rv = c.post(
                "/upload",
                data={"batch_name": "SLOW", "table_text": SUP_A, "note": "慢"},
                follow_redirects=True,
            )
            slow_result["status"] = rv.status_code
            slow_result["body"] = rv.get_data(as_text=True)

        try:
            t = threading.Thread(target=slow_post)
            t.start()
            self.assertTrue(check_started.wait(timeout=5), "慢批校验未开始")

            t0 = time.perf_counter()
            rv = self.app.test_client().post(
                "/upload",
                data={"batch_name": "FAST", "table_text": SUP_B, "note": "快"},
                follow_redirects=True,
            )
            fast_elapsed = time.perf_counter() - t0
            fast_body = rv.get_data(as_text=True)
            self.assertIn("补报成功", fast_body)
            self.assertLess(fast_elapsed, 3.0, "其他批次的补报不应等慢批的锁")

            release.set()
            t.join(timeout=10)
            self.assertFalse(t.is_alive())
            self.assertEqual(slow_result.get("status"), 200)
            self.assertIn("补报成功", slow_result.get("body", ""))
        finally:
            release.set()
            app_db.load_batch_tables = real_load

        self.assertEqual(self._tables("SLOW")[0], 2)
        self.assertEqual(self._tables("FAST")[0], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
