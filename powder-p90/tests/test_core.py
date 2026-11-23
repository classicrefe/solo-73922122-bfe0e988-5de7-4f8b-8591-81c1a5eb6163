"""核心算法测试：校验、相容性、合并 P90 最紧界、见证、10 亿粒性能。

运行：python -m unittest discover -s tests -v
"""
import os
import random
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core import (  # noqa: E402
    BinTableError,
    compatible,
    e_at,
    merge_p90,
    parse_table,
    s_at,
    verify_witness,
)


def table_from_sizes(sizes, partition_edges):
    """把具体粒径序列按给定分档边界（各档 lo 序列，自动补末尾）生成档表文本。"""
    max_size = max(sizes)
    edges = sorted({0, *(e for e in partition_edges if e <= max_size)})
    edges = edges + [max_size + 1]
    lines = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1] - 1
        cnt = sum(1 for s in sizes if lo <= s <= hi)
        lines.append(f"{lo} {hi} {cnt}")
    return parse_table("\n".join(lines))


def brute_feasible(tables, size_hi):
    """独立参考实现：逐整数 x 求各表 CDF 区间交集，贪心判断是否存在非递减 CDF。

    允许零粒尾档扩展覆盖；真正的冲突由各点 L<=U 是否可满足来判定。
    """
    n = tables[0].total
    if any(t.total != n for t in tables):
        return False
    c_lo = min(t.cuts[0] for t in tables)
    c_hi = max(t.cuts[-1] for t in tables)
    chosen = 0
    for x in range(c_lo, c_hi):
        # 「粒径 <= x」的粒数 = 「粒径 < x+1」的粒数
        low = max(s_at(t, x + 1) for t in tables)
        up = min(e_at(t, x + 1) for t in tables)
        chosen = max(chosen, low)
        if chosen > up:
            return False
    return chosen == n


def brute_p90_bounds(batch_tables, size_hi):
    """独立参考实现：逐整数 x 扫描 U/L(x)=各批区间界之和，求最紧 P90 界。"""
    total = sum(ts[0].total for _, ts in batch_tables)
    k = (9 * total + 9) // 10
    # U(x)=「<=x」最大粒数 = sum_b min_T E_T(x+1)；找最小 x 使 U(x) >= k
    lo = hi = None
    for x in range(size_hi + 1):
        run_u = sum(min(e_at(t, x + 1) for t in ts) for _, ts in batch_tables)
        if run_u >= k:
            lo = x
            break
    # L(x)=「<=x」最小粒数 = sum_b max_T S_T(x+1)；找最小 x 使 L(x) >= k
    for x in range(size_hi + 1):
        run_l = sum(max(s_at(t, x + 1) for t in ts) for _, ts in batch_tables)
        if run_l >= k:
            hi = x
            break
    return lo, hi, k, total


class TestParsing(unittest.TestCase):
    def test_basic(self):
        t = parse_table("0 9 5\n10 19 3\n20 29 0\n")
        self.assertEqual(t.total, 8)
        self.assertEqual(t.cuts, (0, 10, 20, 30))
        self.assertEqual(t.cum, (0, 5, 8, 8))

    def test_comma_and_header(self):
        t = parse_table("lo,hi,count\n0,9,5\n10,19,3\n")
        self.assertEqual(t.total, 8)

    def test_errors(self):
        cases = [
            "",
            "0 9 0\n10 19 0",            # 全零
            "0 9 5\n11 19 3",            # 不连续
            "0 9 5\n8 19 3",             # 重叠
            "-1 9 5",                    # 负
            "0 9 -1",                   # 负粒数
            "9 0 5",                    # lo>hi
            "0 9 5\n10 19",             # 字段不足
            "0 9 abc",                  # 非整数
            "lo 9 5",                   # 表头只允许整行非整数，部分非整数报错
        ]
        for text in cases:
            with self.assertRaises(BinTableError):
                parse_table(text)

    def test_nonzero_start_allowed(self):
        t = parse_table("5 9 5\n10 19 3")
        self.assertEqual(t.cuts[0], 5)


class TestCorridor(unittest.TestCase):
    def test_s_e_basic(self):
        t = parse_table("0 9 5\n10 19 3\n20 29 2")
        # cuts 0,10,20,30 ; cum 0,5,8,10
        self.assertEqual((s_at(t, 0), e_at(t, 0)), (0, 0))
        # 切割点上 S=E（CDF 被档表钉死）
        self.assertEqual((s_at(t, 10), e_at(t, 10)), (5, 5))
        # c=15 落在档 [10,19] 内部：S=5（首档必在左侧），E=8（前两档 lo<15）
        self.assertEqual((s_at(t, 15), e_at(t, 15)), (5, 8))
        self.assertEqual((s_at(t, 20), e_at(t, 20)), (8, 8))
        self.assertEqual((s_at(t, 30), e_at(t, 30)), (10, 10))
        self.assertEqual((s_at(t, 35), e_at(t, 35)), (10, 10))
        self.assertEqual((s_at(t, -1), e_at(t, -1)), (0, 0))


class TestCompatibility(unittest.TestCase):
    def test_compatible_refinements(self):
        sizes = [0, 0, 5, 10, 10, 15, 20, 25]
        t1 = table_from_sizes(sizes, [0, 10, 20])
        t2 = table_from_sizes(sizes, [0, 5, 15, 25])
        t3 = table_from_sizes(sizes, [0, 3, 7, 12, 18, 22])
        ok, _ = compatible([t1, t2, t3])
        self.assertTrue(ok)
        self.assertTrue(brute_feasible([t1, t2, t3], 30))

    def test_incompatible_total(self):
        t1 = parse_table("0 9 5\n10 19 3")
        t2 = parse_table("0 9 5\n10 19 4")
        ok, reason = compatible([t1, t2])
        self.assertFalse(ok)
        self.assertIn("总粒数", reason)

    def test_incompatible_split(self):
        # 同一总粒数但分档冲突：[0,9] 报 8 粒 vs 细分后 [0,4] 就有 10 粒
        t1 = parse_table("0 9 8\n10 19 2")
        t2 = parse_table("0 4 10\n5 9 0\n10 14 0\n15 19 0")
        ok, _ = compatible([t1, t2])
        self.assertFalse(ok)
        self.assertFalse(brute_feasible([t1, t2], 20))

    def test_extended_range_with_zero_bin_is_compatible(self):
        # 多报一张带零粒尾档的表，覆盖范围更大但不冲突，应放行
        t1 = parse_table("0 9 5\n10 19 3")
        t2 = parse_table("0 9 5\n10 19 3\n20 29 0")
        ok, _ = compatible([t1, t2])
        self.assertTrue(ok)
        self.assertTrue(brute_feasible([t1, t2], 30))

    def test_incompatible_range(self):
        # 总粒数 8。在切割点 c=10（粒径 <10 的粒数）处：
        #   t2 的 [0,4] 档有 7 粒 => S_t2(10)=7（至少 7 粒 <10）；
        #   t1 的 [0,9] 档只有 3 粒，[10,19] 有 5 粒 => E_t1(10)=3（至多 3 粒 <10）。
        # L(10)=7 > U(10)=3，两表不可能来自同一组粒子。
        t1 = parse_table("0 9 3\n10 19 5")
        t2 = parse_table("0 4 7\n5 9 0\n10 14 1\n15 19 0")
        ok, reason = compatible([t1, t2])
        self.assertFalse(ok)
        self.assertIn("冲突", reason)
        self.assertFalse(brute_feasible([t1, t2], 30))


class TestMergeP90(unittest.TestCase):
    def test_hand_computed_two_batches(self):
        # 批 1：900 粒，两表相容
        b1_t1 = parse_table("0 99 400\n100 199 500")
        b1_t2 = parse_table("0 49 100\n50 99 300\n100 149 400\n150 199 100")
        ok, _ = compatible([b1_t1, b1_t2])
        self.assertTrue(ok)
        # 批 2：1000 粒，单表
        b2_t1 = parse_table("0 100 500\n101 200 500")
        res = merge_p90([(1, [b1_t1, b1_t2]), (2, [b2_t1])])
        self.assertEqual(res.total, 1900)
        self.assertEqual(res.k, 1710)
        # 手工：cuts 0,50,100,101,150,200,201
        # U 累计：500,900,1400(=900+500 在 <=100),1900... k=1710 首次达到在 [101,150) -> 101
        # L 累计：100,400,900,900,1800 -> 首次 >=1710 在 [150,200] -> 200
        self.assertEqual(res.p90_low, 101)
        self.assertEqual(res.p90_high, 200)
        # 见证逐批校验
        for bid, freq in res.witness_low.items():
            verify_witness(freq, [b1_t1, b1_t2] if bid == 1 else [b2_t1])
        for bid, freq in res.witness_high.items():
            verify_witness(freq, [b1_t1, b1_t2] if bid == 1 else [b2_t1])

    def test_single_table_p90_pinpoints(self):
        # 单表单档宽区间：P90 界就是整个档（信息最少）
        t = parse_table("0 100 1000")
        res = merge_p90([(7, [t])])
        self.assertEqual(res.k, 900)
        self.assertEqual((res.p90_low, res.p90_high), (0, 100))

    def test_single_size_table(self):
        t = parse_table("42 42 10")
        res = merge_p90([(1, [t])])
        self.assertEqual((res.p90_low, res.p90_high, res.k), (42, 42, 9))


class TestFuzz(unittest.TestCase):
    def test_random(self):
        rng = random.Random(20261123)
        for trial in range(300):
            n_batches = rng.randint(1, 3)
            batch_tables = []
            batch_sizes = {}
            size_hi = rng.randint(8, 28)
            for bid in range(1, n_batches + 1):
                n = rng.randint(1, 60)
                sizes = [rng.randint(0, size_hi) for _ in range(n)]
                batch_sizes[bid] = sizes
                n_tables = rng.randint(1, 3)
                tables = []
                for _ in range(n_tables):
                    edges = [0] + sorted(rng.sample(range(1, size_hi + 1), rng.randint(1, 4)))
                    tables.append(table_from_sizes(sizes, edges))
                ok, _ = compatible(tables)
                self.assertTrue(ok, f"trial {trial}: 同源档表误判不相容")
                self.assertTrue(brute_feasible(tables, size_hi))
                batch_tables.append((bid, tables))

            res = merge_p90(batch_tables)
            lo, hi, k, total = brute_p90_bounds(batch_tables, size_hi)
            self.assertEqual(res.k, k)
            self.assertEqual(res.total, total)
            self.assertEqual(res.p90_low, lo, f"trial {trial} 下界不符")
            self.assertEqual(res.p90_high, hi, f"trial {trial} 上界不符")
            self.assertLessEqual(res.p90_low, res.p90_high)

            # 两份见证的合并 P90 必须恰好等于所声称的端点
            from app.core import _order_stat, batch_bounds, _union_cuts
            self.assertEqual(_order_stat(res.witness_low, k), res.p90_low)
            self.assertEqual(_order_stat(res.witness_high, k), res.p90_high)
            for bid, tables in batch_tables:
                verify_witness(res.witness_low[bid], tables)
                verify_witness(res.witness_high[bid], tables)

            # 见证 CDF 在每个全局切割点上必须贴住该批的 U/L 走廊
            gcuts = list(res.cuts)
            for bid, tables in batch_tables:
                low_b, up_b = batch_bounds(tables, gcuts)
                cdf_lo = cdf_hi = 0
                for j, c in enumerate(gcuts):
                    cdf_lo = sum(v for s, v in res.witness_low[bid].items() if s < c)
                    cdf_hi = sum(v for s, v in res.witness_high[bid].items() if s < c)
                    self.assertEqual(cdf_lo, up_b[j], f"trial {trial} 下界见证未贴 U 界")
                    self.assertEqual(cdf_hi, low_b[j], f"trial {trial} 上界见证未贴 L 界")

            # 用各批的原始真实粒子组（生成档表的同一组粒径）合并，
            # 其 P90 必须落在声称的最紧界内
            real_merged: dict[int, int] = {}
            for bid, _ in batch_tables:
                for s in batch_sizes[bid]:
                    real_merged[s] = real_merged.get(s, 0) + 1
            real_p90 = _order_stat({0: real_merged}, k)
            self.assertLessEqual(res.p90_low, real_p90)
            self.assertLessEqual(real_p90, res.p90_high)

    def test_random_incompatible(self):
        rng = random.Random(99)
        mismatches = 0
        for _ in range(200):
            size_hi = 12
            n = rng.randint(2, 30)
            sizes = [rng.randint(0, size_hi) for _ in range(n)]
            t1 = table_from_sizes(sizes, [0, rng.randint(2, 6)])
            # 造一张随机档表（不一定同源），总粒数凑成相同
            edges = [0] + sorted(rng.sample(range(1, size_hi + 1), rng.randint(1, 3)))
            rows = []
            ed = edges + [size_hi + 1]
            pool = n
            for i in range(len(ed) - 1):
                c = rng.randint(0, pool) if i < len(ed) - 2 else pool
                rows.append(f"{ed[i]} {ed[i+1]-1} {c}")
                pool -= c
            t2 = parse_table("\n".join(rows))
            got, _ = compatible([t1, t2])
            want = brute_feasible([t1, t2], size_hi)
            self.assertEqual(got, want)
            if not got:
                mismatches += 1
        self.assertGreater(mismatches, 0)  # 确实测到了拒绝案例


class TestBillionScale(unittest.TestCase):
    def test_billion_particles_by_bin(self):
        # 两批合计 10 亿粒，档数很少；必须按档计算且快速完成
        b1 = parse_table("0 99 400000000\n100 199 500000000")          # 9 亿
        b2 = parse_table("0 100 50000000\n101 200 50000000")           # 1 亿
        t0 = time.perf_counter()
        res = merge_p90([(1, [b1]), (2, [b2])])
        elapsed = time.perf_counter() - t0
        self.assertEqual(res.total, 1_000_000_000)
        self.assertEqual(res.k, 900_000_000)
        # cuts=[0,100,101,200,201]：U(100)=4.5亿<9亿；U(101)=9亿>=9亿 -> 下界 100
        self.assertEqual(res.p90_low, 100)
        # L(200)=9.5亿>=9亿 -> 段 [101,200) 右端=199；且 b1 上限就是 199
        self.assertEqual(res.p90_high, 199)
        self.assertLess(elapsed, 1.0)
        # 见证粒数合计 10 亿，且逐批符合档表
        self.assertEqual(sum(sum(f.values()) for f in res.witness_low.values()), 1_000_000_000)
        verify_witness(res.witness_low[1], [b1])
        verify_witness(res.witness_high[2], [b2])

    def test_many_bins_linear_time(self):
        # 多档数（每批 2 万档、合计 10 亿粒）必须按档线性计算，秒级完成，
        # 见证频数规模与档数同阶而非与粒数同阶。
        rng = random.Random(11)
        n_bins, total = 20_000, 500_000_000

        def make(seed):
            rnd = random.Random(seed)
            rows = []
            lo = 0
            remaining = total
            for i in range(n_bins):
                hi = lo + 9
                cnt = (
                    remaining
                    if i == n_bins - 1
                    else rnd.randint(0, min(remaining, total // n_bins * 3))
                )
                rows.append(f"{lo} {hi} {cnt}")
                remaining -= cnt
                lo = hi + 1
            return parse_table("\n".join(rows))

        b1, b2 = make(1), make(2)
        t0 = time.perf_counter()
        res = merge_p90([(1, [b1]), (2, [b2])])
        elapsed = time.perf_counter() - t0
        self.assertEqual(res.total, 1_000_000_000)
        self.assertEqual(res.k, 900_000_000)
        self.assertLessEqual(res.p90_low, res.p90_high)
        self.assertLess(elapsed, 5.0)
        witness_entries = sum(len(f) for f in res.witness_low.values())
        self.assertLessEqual(witness_entries, 2 * n_bins)


if __name__ == "__main__":
    unittest.main(verbosity=2)
