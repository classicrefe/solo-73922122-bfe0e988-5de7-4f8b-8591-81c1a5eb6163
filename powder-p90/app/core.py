"""
粉体粒径档表：相容性判定与合并 P90 最紧界（全程按档计算，不逐粒展开）。

术语
----
档 (bin)：连续排列、互不重叠的整数微米闭区间 [lo, hi]，lo <= hi 均为非负整数，
          粒数 count 为非负整数，整表至少一粒。
档表 (table)：同一批粒子的一次统计结果。同一次统计的档首尾相接、按 lo 升序。
批 (batch)：同一组粒子；同批可补报多张由该同一组粒子得到的不同档表，
            所有已入库档表必须整体相容（能由同一组粒径产生）。

核心事实
--------
对一张档表 T，切割点 cuts = [b0.lo, b0.hi+1, b1.hi+1, ..., bm.hi+1]，
累计粒数 cum[i] = 前 i 个档的粒数和（cum[0]=0, cum[m]=N）。

对任意整数切割点 c，任一相容粒子组中「粒径 < c 的粒数」被夹在：
  S_T(c) = 完全在 c 左侧（hi+1 <= c）的档粒数和（下界）；
  E_T(c) = 起点 lo 严格小于 c 的档粒数和（上界：lo=c 的档内粒径均 >=c，
           不可能计入「<c」，故用 lo<c 而非 lo<=c）。
在表自身的切割点 cuts[i] 上有 S_T=E_T=cum[i]，该点 CDF 被档表钉死，
走廊宽度只出现在档内部。

把一批所有档表的切割点取并集 c_0 < c_1 < ... < c_r，该批在切割点 j 处的累计走廊：
  L_b(j) = max_T S_T(c_j)     （粒径 < c_j 的最小可能粒数）
  U_b(j) = min_T E_T(c_j)     （粒径 < c_j 的最大可能粒数）
多表相容 ⇔ 总粒数一致且每个切割点上 L_b(j) <= U_b(j)（L、U 均单调不减）。

合并多批时各批独立，合并走廊在切割点 j 处为 L(j)=Σ_b L_b(j)、U(j)=Σ_b U_b(j)。
P90 = 升序第 k 粒（k = ceil(0.9 N)）：
  最紧下界 = 首个满足 U(j+1) >= k 的段 [c_j, c_{j+1}) 的左端点 c_j；
  最紧上界 = 首个满足 L(j+1) >= k 的段的右端点 c_{j+1}-1。
两份逐批见证（同一段向量两种堆法，均逐表可行）：
  下界见证取 U 的逐段差分 U(j+1)-U(j)、粒子堆段左端，CDF 处处贴 U（最靠左）；
  上界见证取 L 的逐段差分 L(j+1)-L(j)、粒子堆段右端，CDF 处处贴 L（最靠右）；
合并后第 k 粒恰好取到对应端点。全部运算只涉及档数规模的整数。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

MAX_MICRON = 2_000_000_000  # 粒径为非负整数微米，给出合理上界


class BinTableError(ValueError):
    """档表解析或校验错误。"""


@dataclass(frozen=True)
class Bin:
    lo: int
    hi: int
    count: int


@dataclass(frozen=True)
class BinTable:
    bins: tuple[Bin, ...]
    total: int
    cuts: tuple[int, ...]   # 长度 m+1，cuts[0]=bins[0].lo，cuts[i]=bins[i-1].hi+1
    cum: tuple[int, ...]    # 长度 m+1，cum[0]=0，cum[i]=前 i 档粒数和


def _as_int(token: str, field: str, lineno: int) -> int:
    s = token.strip()
    if not s:
        raise BinTableError(f"第 {lineno} 行存在空字段（{field}）")
    try:
        return int(s)
    except ValueError as exc:
        raise BinTableError(f"第 {lineno} 行 {field}={s!r} 不是整数") from exc


def parse_table(text: str) -> BinTable:
    """解析档表文本。

    每行一档：lo hi count，空白或逗号分隔。
    若一行三个字段都不是整数，视为表头/注释行跳过；部分字段非整数则报错。
    校验：非负整数、lo<=hi、按 lo 升序首尾相接（前档 hi+1 == 次档 lo）、
    count 非负、整表至少一粒、粒径不超过 MAX_MICRON。
    """
    raw_bins: list[Bin] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = [p for p in line.replace(",", " ").split() if p]
        if len(parts) != 3:
            raise BinTableError(
                f"第 {lineno} 行应有 3 个字段 lo hi count，实际 {len(parts)} 个：{line!r}"
            )
        # 整行都是非整数字段时按表头跳过
        if all(not _looks_like_int(p) for p in parts):
            continue
        lo = _as_int(parts[0], "lo", lineno)
        hi = _as_int(parts[1], "hi", lineno)
        count = _as_int(parts[2], "count", lineno)
        if lo < 0 or hi < 0 or count < 0:
            raise BinTableError(f"第 {lineno} 行 lo/hi/count 必须非负：{line!r}")
        if lo > hi:
            raise BinTableError(f"第 {lineno} 行 lo({lo}) > hi({hi})")
        if hi > MAX_MICRON:
            raise BinTableError(f"第 {lineno} 行 hi={hi} 超出允许范围（0..{MAX_MICRON}）")
        raw_bins.append(Bin(lo, hi, count))

    if not raw_bins:
        raise BinTableError("档表为空，至少需要一档")
    if sum(b.count for b in raw_bins) <= 0:
        raise BinTableError("整表粒数为 0，整表至少一粒")

    bins: list[Bin] = []
    prev_hi = None
    for idx, b in enumerate(raw_bins):
        if prev_hi is not None and b.lo != prev_hi + 1:
            raise BinTableError(
                f"第 {idx + 1} 档起点 {b.lo} 与前档终点 {prev_hi} 不连续"
                "（档必须连续排列、互不重叠）"
            )
        bins.append(b)
        prev_hi = b.hi

    cuts = [bins[0].lo]
    cum = [0]
    total = 0
    for b in bins:
        cuts.append(b.hi + 1)
        total += b.count
        cum.append(total)
    return BinTable(tuple(bins), total, tuple(cuts), tuple(cum))


def _looks_like_int(token: str) -> bool:
    s = token.strip()
    if not s:
        return False
    if s[0] in "+-":
        s = s[1:]
    return s.isdigit()


# ---------------------------------------------------------------------------
# 单表在切割点处的累计走廊界（按档二分，O(log 档数)）
# ---------------------------------------------------------------------------

def s_at(table: BinTable, c: int) -> int:
    """S_T(c)：完全在 c 左侧（hi+1 <= c）的档粒数和，即「粒径 < c」粒数下界。"""
    i = bisect.bisect_right(table.cuts, c) - 1  # 满足 cuts[i] <= c 的最大 i
    if i <= 0:
        return 0
    return table.cum[min(i, len(table.bins))]


def e_at(table: BinTable, c: int) -> int:
    """E_T(c)：可能落在 c 左侧（粒径 < c）的最大粒数上界。

    档 [lo,hi] 内可能出现粒径 < c 当且仅当 lo < c（lo=c 时档内粒径均 >=c），
    故 E_T(c) = 起点 lo 严格小于 c 的档粒数和 = bisect_left(los, c) 对应累计。
    c <= 首档 lo 时为 0；c > 末档 lo 时为全表总粒数。
    在表自身的切割点 cuts[i] 上有 S_T(cuts[i]) = E_T(cuts[i]) = cum[i]，
    即该点的 CDF 被档表钉死，走廊宽度只出现在档内部。
    """
    los = table.cuts[:-1]
    p = bisect.bisect_left(los, c)
    return table.cum[p]


def _union_cuts(tables: list[BinTable]) -> list[int]:
    cut_set: set[int] = set()
    for t in tables:
        cut_set.update(t.cuts)
    return sorted(cut_set)


# ---------------------------------------------------------------------------
# 单表在一组（升序）切割点上的 S/E 界：双指针线性归并，O(len(cuts)+档数)
# ---------------------------------------------------------------------------

def table_bounds_on_cuts(
    table: BinTable, cuts: list[int]
) -> tuple[list[int], list[int]]:
    """返回表 table 在每个切割点 c 上的 (S_T(c), E_T(c)) 列表，线性扫描无二分。

    S_T(c)：cuts_table 中 <= c 的最右切割点对应累计（hi+1<=c 的档已完全在左侧）；
    E_T(c)：各档 lo 严格 < c 的档累计（lo<c 的档才可能含粒径 <c 的粒子）。
    """
    m = len(table.bins)
    s_list = [0] * len(cuts)
    e_list = [0] * len(cuts)
    i = 0  # 指向 table.cuts，求 <= c 的最右位置
    p = 0  # 指向各档 lo（table.cuts[:-1]），求 < c 的个数
    for j, c in enumerate(cuts):
        while i < len(table.cuts) and table.cuts[i] <= c:
            i += 1
        # i 为首个 > c 的位置，<=c 的有 i 个；S= cum[min(i, m)]
        s_list[j] = table.cum[i - 1] if i > 0 else 0
        if i > m:
            s_list[j] = table.cum[m]
        while p < m and table.cuts[p] < c:
            p += 1
        e_list[j] = table.cum[p]
    return s_list, e_list


# ---------------------------------------------------------------------------
# 一批在切割点序列上的累计走廊 L(j)/U(j)
# ---------------------------------------------------------------------------

def batch_bounds(tables: list[BinTable], cuts: list[int]) -> tuple[list[int], list[int]]:
    """返回 (L, U)：在每个切割点 c_j 处「粒径 < c_j」粒数的最小/最大可能值。

    L = 各表 S 界逐点最大；U = 各表 E 界逐点最小。先线性归并出每张表的
    S/E 序列，再逐点归并，整体 O(表数 × (档数 + 全局切割点数))。
    """
    n = len(cuts)
    low = [0] * n
    up = [0] * n
    for ti, t in enumerate(tables):
        s_list, e_list = table_bounds_on_cuts(t, cuts)
        if ti == 0:
            for j in range(n):
                low[j] = s_list[j]
                up[j] = e_list[j]
        else:
            for j in range(n):
                if s_list[j] > low[j]:
                    low[j] = s_list[j]
                if e_list[j] < up[j]:
                    up[j] = e_list[j]
    return low, up


# ---------------------------------------------------------------------------
# 多表相容性（同批补报校验）
# ---------------------------------------------------------------------------

def compatible(tables: list[BinTable]) -> tuple[bool, str]:
    """判断若干张档表能否由同一组粒径得到。

    条件：
      1. 总粒数相同；
      2. 在所有表切割点并集的每个切割点 c 上，max_T S_T(c) <= min_T E_T(c)。
    覆盖范围不同但多出的区间各表都允许零粒时，条件 2 自然放行；真正冲突会被
    条件 2 拦住（零粒尾档不要求覆盖范围完全一致）。
    """
    if not tables:
        return False, "没有档表"
    first = tables[0]
    for idx, t in enumerate(tables[1:], start=1):
        if t.total != first.total:
            return False, (
                f"总粒数不一致：表 1 为 {first.total} 粒，表 {idx + 1} 为 {t.total} 粒"
            )

    # 覆盖范围：取所有表覆盖的并集 [c_lo, c_hi)。允许某张表用零粒尾档把并集
    # 往外扩（不要求新粒子）；但某张表若缺少并集中的某段覆盖，落在该段的粒子
    # 无法被它统计，真正的冲突随后由走廊 L<=U 判定拦住。这里不再硬性要求
    # 每张表覆盖整个并集。
    cuts = _union_cuts(tables)
    low, up = batch_bounds(tables, cuts)
    for j, c in enumerate(cuts):
        if low[j] > up[j]:
            return False, (
                "档表冲突：在切割点 c=%d（粒径 <%d 的粒数）处，"
                "至少须有 %d 粒，但至多只能有 %d 粒" % (c, c, low[j], up[j])
            )
    if low[-1] != first.total or up[-1] != first.total:
        return False, "档表覆盖范围外仍有粒子，无法由同一组粒径得到"
    return True, ""

# ---------------------------------------------------------------------------
# 见证校验：一份粒径频数是否符合本批所有档表（按档归并，不逐粒展开）
# ---------------------------------------------------------------------------

def verify_witness(freq: dict[int, int], tables: list[BinTable]) -> None:
    """断言频数 freq={粒径: 粒数} 满足本批每张档表的逐档粒数。

    频数键与各档均按粒径有序，用双指针线性归并，不逐粒展开。
    """
    total = sum(freq.values())
    ordered = sorted(freq.items())
    for t in tables:
        if total != t.total:
            raise AssertionError(f"见证总粒数 {total} != 档表总粒数 {t.total}")
        per_bin = [0] * len(t.bins)
        idx = 0
        for size, cnt in ordered:
            if cnt < 0:
                raise AssertionError("见证出现负粒数")
            while idx < len(t.bins) and size > t.bins[idx].hi:
                idx += 1
            if idx >= len(t.bins) or size < t.bins[idx].lo:
                raise AssertionError(f"粒径 {size} 落在档表覆盖范围之外")
            per_bin[idx] += cnt
        for i, b in enumerate(t.bins):
            if per_bin[i] != b.count:
                raise AssertionError(
                    f"档 [{b.lo},{b.hi}] 归并粒数 {per_bin[i]} != 档表粒数 {b.count}"
                )


def _order_stat(freqs: dict[int, dict[int, int]], k: int) -> int:
    """合并各批频数，求升序第 k 小粒径（k 从 1 开始）。按粒径聚合，不逐粒展开。"""
    merged: dict[int, int] = {}
    for freq in freqs.values():
        for size, cnt in freq.items():
            merged[size] = merged.get(size, 0) + cnt
    running = 0
    for size in sorted(merged):
        running += merged[size]
        if running >= k:
            return size
    raise RuntimeError("internal: k exceeds total particles")


# ---------------------------------------------------------------------------
# 合并 P90 最紧界
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MergeResult:
    batch_ids: tuple[int, ...]
    total: int
    k: int
    p90_low: int
    p90_high: int
    cuts: tuple[int, ...]
    merge_low: tuple[int, ...]   # L(j)：切割点 j 处合并累计粒数下界
    merge_up: tuple[int, ...]    # U(j)：切割点 j 处合并累计粒数上界
    witness_low: dict[int, dict[int, int]]   # batch_id -> {size: count}，达到 p90_low
    witness_high: dict[int, dict[int, int]]  # 达到 p90_high


def merge_p90(batch_tables: list[tuple[int, list[BinTable]]]) -> MergeResult:
    """对勾选的多批（每批给出全部已入库档表）计算合并 P90 的最紧上下界及见证。

    batch_tables: [(batch_id, [BinTable, ...]), ...]，每批至少一张已入库档表。
    """
    if not batch_tables:
        raise BinTableError("未选择任何批次")

    all_tables = [t for _, ts in batch_tables for t in ts]
    cuts = _union_cuts(all_tables)
    r = len(cuts) - 1

    bounds: dict[int, tuple[list[int], list[int]]] = {}
    totals = {bid: ts[0].total for bid, ts in batch_tables}
    for batch_id, tables in batch_tables:
        # 一次线性归并同时得到该批在全局切割点上的 L/U，并就地做相容校验，
        # 避免 compatible() 再求一次切割点并集与逐点二分。
        if any(t.total != totals[batch_id] for t in tables):
            raise BinTableError(f"批次 {batch_id} 的档表总粒数不一致")
        low, up = batch_bounds(tables, cuts)
        for j in range(len(cuts)):
            if low[j] > up[j]:
                raise BinTableError(
                    f"批次 {batch_id} 的已入库档表不相容：切割点 c={cuts[j]} "
                    f"（粒径 <{cuts[j]} 的粒数）至少 {low[j]} 粒、至多 {up[j]} 粒"
                )
        if low[-1] != totals[batch_id] or up[-1] != totals[batch_id]:
            raise BinTableError(f"批次 {batch_id} 的档表覆盖范围外仍有粒子")
        bounds[batch_id] = (low, up)

    total = sum(ts[0].total for _, ts in batch_tables)
    if total <= 0:
        raise BinTableError("合并总粒数为 0")
    k = (9 * total + 9) // 10  # ceil(0.9 N) = ceil(9N/10)

    merge_low = [0] * len(cuts)
    merge_up = [0] * len(cuts)
    for _, (low, up) in bounds.items():
        for j in range(len(cuts)):
            merge_low[j] += low[j]
            merge_up[j] += up[j]

    # 最紧下界：首个 U(j+1) >= k 的段，粒子可落在段左端 c_j
    p90_low = None
    for j in range(r):
        if merge_up[j + 1] >= k:
            p90_low = cuts[j]
            break
    # 最紧上界：首个 L(j+1) >= k 的段，粒子最迟落在段右端 c_{j+1}-1
    p90_high = None
    for j in range(r):
        if merge_low[j + 1] >= k:
            p90_high = cuts[j + 1] - 1
            break
    if p90_low is None or p90_high is None:
        raise RuntimeError("internal: P90 segment not found")
    if p90_low > p90_high:
        raise RuntimeError("internal: inconsistent P90 bounds")

    # 逐批见证（L/U 为该批在全局切割点上的累计走廊，段 j 区间 [c_j, c_{j+1})）：
    #   下界见证——CDF 处处贴 U（最靠左），粒子堆段左端 c_j；
    #   上界见证——CDF 处处贴 L（最靠右），粒子堆段右端 c_{j+1}-1。
    witness_low: dict[int, dict[int, int]] = {}
    witness_high: dict[int, dict[int, int]] = {}
    for batch_id, tables in batch_tables:
        low, up = bounds[batch_id]
        total_b = tables[0].total
        if low[-1] != total_b or up[-1] != total_b:
            raise RuntimeError("internal: batch corridor does not close at total")
        fl: dict[int, int] = {}
        fh: dict[int, int] = {}

        # 切割点 j 处该批的累计走廊为 [L(j), U(j)]，且 L(r)=U(r)=N。
        # 两条边界各自对应可行的逐段向量：
        #   u_j = U(j+1)-U(j)（U 界差分）、l_j = L(j+1)-L(j)（L 界差分）。
        # 因为每张表在自身切割点上 S=E（CDF 被钉死），这两个向量都逐表可行
        # （由 verify_witness 交叉断言）。
        # 下界见证（合并 P90 取最小）：段 j 放 u_j，粒子堆段左端 c_j，
        #   每个切割点处累计恰为 U，CDF 整体最靠左；
        # 上界见证（合并 P90 取最大）：段 j 放 l_j，粒子堆段右端 c_{j+1}-1，
        #   每个切割点处累计恰为 L，CDF 整体最靠右。
        for j in range(r):
            u_j = up[j + 1] - up[j]
            l_j = low[j + 1] - low[j]
            if u_j < 0 or l_j < 0:
                raise RuntimeError("internal: witness negative segment")
            if u_j:
                fl[cuts[j]] = fl.get(cuts[j], 0) + u_j
            if l_j:
                fh[cuts[j + 1] - 1] = fh.get(cuts[j + 1] - 1, 0) + l_j

        if sum(fl.values()) != total_b or sum(fh.values()) != total_b:
            raise RuntimeError("internal: witness totals mismatch")
        verify_witness(fl, tables)
        verify_witness(fh, tables)
        witness_low[batch_id] = fl
        witness_high[batch_id] = fh

    if _order_stat(witness_low, k) != p90_low:
        raise RuntimeError("internal: lower endpoint witness failed")
    if _order_stat(witness_high, k) != p90_high:
        raise RuntimeError("internal: upper endpoint witness failed")

    return MergeResult(
        batch_ids=tuple(b for b, _ in batch_tables),
        total=total,
        k=k,
        p90_low=p90_low,
        p90_high=p90_high,
        cuts=tuple(cuts),
        merge_low=tuple(merge_low),
        merge_up=tuple(merge_up),
        witness_low=witness_low,
        witness_high=witness_high,
    )
