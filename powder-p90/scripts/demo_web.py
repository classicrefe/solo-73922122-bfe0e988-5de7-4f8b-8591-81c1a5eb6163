"""通过网页接口演示：新批号首报、相容补报、冲突补报被退回、并发同批上传。

用法（服务已启动）：
    python -m scripts.demo_web [服务地址]
默认 http://127.0.0.1:8000。脚本只调用 /upload 与 /compute 网页接口，
不直接改数据库，便于在 Docker Compose（两个工作进程）下复现。

每次运行使用带时间戳的批号，避免与已有数据冲突。
"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

FIRST = "0 99 400\n100 199 500\n"
FINE = "0 49 100\n50 99 300\n100 149 400\n150 199 100\n"
BAD = "0 49 450\n50 99 0\n100 199 450\n"
RACE_OLD = "0 9 100\n10 19 100\n"
RACE_A = "0 4 50\n5 9 50\n10 19 100\n"
RACE_B = "0 4 90\n5 9 10\n10 19 100\n"


def _post(base: str, path: str, fields: dict[str, str]) -> str:
    """POST 并跟随重定向（带 cookie，flash 在下一次 GET 的页面里）。

    每个调用用独立 cookie jar，避免并发线程共用会话互相覆盖 flash。
    """
    import http.cookiejar

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(base + path, data=data, method="POST")
    with opener.open(req, timeout=30) as resp:  # 自动跟随 302 到 /
        return resp.read().decode()


def _flash(body: str) -> str:
    """从返回的首页/结果页里抽取 flash 文本（去标签）。"""
    import html
    import re

    msgs = re.findall(r'<div class="flash [^"]+">(.*?)</div>', body, re.S)
    return " | ".join(html.unescape(re.sub(r"\s+", " ", m)).strip() for m in msgs)


def upload(base: str, name: str, text: str, note: str = "") -> str:
    body = _post(base, "/upload", {"batch_name": name, "table_text": text, "note": note})
    return _flash(body)


def main(base: str) -> None:
    stamp = time.strftime("%m%d-%H%M%S")
    ok_all = True

    print("=" * 68)
    print("① 新批号首报：只建一个批次")
    demo = f"DEMO-{stamp}"
    print("  ", upload(base, demo, FIRST, "首报"))

    print("② 相容补报：两张首报相容，都归入同一批")
    print("  ", upload(base, demo, FINE, "细分补报"))

    print("③ 冲突补报：整张退回并提示冲突切割点")
    print("  ", upload(base, demo, BAD, "冲突示例"))

    print("④ 并发同批上传（两个以上请求同时到达）")
    race = f"RACE-{stamp}"
    print("  旧表:", upload(base, race, RACE_OLD, "旧表"))
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [
            ex.submit(upload, base, race, RACE_A, "并发 A"),
            ex.submit(upload, base, race, RACE_B, "并发 B"),
        ]
        outcomes = [f.result() for f in futs]
    for msg in outcomes:
        print("   ", msg)
    accepted = sum("补报成功" in m for m in outcomes)
    rejected = sum("补报被拒绝" in m and "冲突" in m for m in outcomes)
    ok_all &= accepted == 1 and rejected == 1
    print(f"  结果：收下 {accepted} 张、退回 {rejected} 张（期望 1 / 1）")

    print("=" * 68)
    print("⑤ 核算演示批次（网页 /compute 接口，结果页含 P90 上下界）")
    body = _post(base, "/compute", {})  # 无选择时应提示
    print("  空选择提示:", _flash(body))
    print("OK" if ok_all else "并发结果不符合预期")
    if not ok_all:
        raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000")
