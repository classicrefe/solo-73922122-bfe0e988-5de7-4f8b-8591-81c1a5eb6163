"""演示用种子数据：清空并重建数据库后写入三批档表。

批次设计：
  B-ALPHA ：两张相容的不同分档表（同一组粒子的初报 + 细分补报）；
  B-BETA  ：单张宽档表，P90 界天然较宽，体现区间不确定性；
  B-GAMMA ：小批量，便于页面上肉眼核对见证频数。
另打印一条“不相容补报”的示例文本，供在网页上手动验证拒绝逻辑。

用法：python -m scripts.seed [数据库路径]
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import BinTableError, compatible, parse_table  # noqa: E402
from app.db import (  # noqa: E402
    connect,
    create_batch,
    get_batch_by_name,
    init_db,
    insert_table,
    load_batch_tables,
)

B_ALPHA_T1 = """lo hi count
0 99 400
100 199 500
"""

B_ALPHA_T2 = """lo hi count
0 49 100
50 99 300
100 149 400
150 199 100
"""

# 与 B-ALPHA 不相容的补报：[0,99] 只有 400 粒，却声称 [0,49] 就有 450 粒
B_ALPHA_BAD = """lo hi count
0 49 450
50 99 0
100 199 450
"""

B_BETA_T1 = """lo hi count
0 100 500
101 200 500
"""

B_GAMMA_T1 = """lo hi count
0 9 20
10 19 30
20 29 50
"""


def main(db_path: str) -> None:
    if os.path.exists(db_path):
        os.remove(db_path)
    init_db(db_path)
    with connect(db_path) as conn:
        for name, texts, note in [
            ("B-ALPHA", [B_ALPHA_T1, B_ALPHA_T2], "初报+细分补报（相容）"),
            ("B-BETA", [B_BETA_T1], "宽档单表"),
            ("B-GAMMA", [B_GAMMA_T1], "小批量"),
        ]:
            batch_id = create_batch(conn, name)
            for i, text in enumerate(texts):
                table = parse_table(text)
                if i > 0:
                    existing = load_batch_tables(conn, batch_id)
                    ok, reason = compatible(existing + [table])
                    if not ok:
                        raise RuntimeError(f"种子数据自相矛盾：{name} {reason}")
                insert_table(conn, batch_id, table, f"{note} #{i + 1}")
            print(f"已写入 {name}：{len(texts)} 张档表")

    # 验证不相容示例确实会被拒绝
    bad = parse_table(B_ALPHA_BAD)
    with connect(db_path) as conn:
        row = get_batch_by_name(conn, "B-ALPHA")
        existing = load_batch_tables(conn, int(row["id"]))
    ok, reason = compatible(existing + [bad])
    assert not ok, "不相容示例应被拒绝"
    print("\n网页演示步骤：")
    print("1. 对 B-ALPHA 粘贴以下补报，应被拒绝且不入库：")
    print(B_ALPHA_BAD)
    print(f"   拒绝原因：{reason}")
    print("2. 勾选 B-ALPHA / B-BETA / B-GAMMA，点“核算合并 P90 最紧界”，")
    print("   页面给出最紧上下界及两份逐批粒径频数见证。")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "powder.db"
    )
    main(path)
