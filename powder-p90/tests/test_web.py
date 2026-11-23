"""Flask 网页端到端测试：上传、相容/不相容补报、勾选核算、见证展示。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.web import create_app  # noqa: E402

ALPHA_T1 = "0 99 400\n100 199 500\n"
ALPHA_T2 = "0 49 100\n50 99 300\n100 149 400\n150 199 100\n"
ALPHA_BAD = "0 49 450\n50 99 0\n100 199 450\n"
BETA_T1 = "0 100 500\n101 200 500\n"
GAMMA_T1 = "0 9 20\n10 19 30\n20 29 50\n"


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
