"""
DeepPack3D 启发式算法基准适配器

将参考项目 SIMPAC-2024-311 (DeepPack3D) 的 4 种启发式算法
(BL / BAF / BSSF / BLSF) 应用于当前项目的 Excel 箱子数据，输出可对比指标。

说明：
- 参考算法原本面向 32³ 整数网格 + 在线传送带模型，与当前项目的 mm 坐标 +
  业务约束（指数、吸盘、间隙等）并不相同。
- 本模块通过等比缩放把托盘和箱子映射到有限整数网格，以便复用参考代码；
  对比结果应理解为「空间装箱思路参考」，不是 WCS 可直接上线的方案。
- RL 方法需要 TensorFlow 2.10 及预训练模型 ./models/k={lookahead}.h5，
  参考仓库未附带模型，默认跳过。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

CODE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CODE_ROOT.parent
REFERENCE_ROOT = PROJECT_ROOT.parent / "参考" / "SIMPAC-2024-311-main"

HEURISTIC_METHODS = ("bl", "baf", "bssf", "blsf")
ALL_METHODS = HEURISTIC_METHODS + ("rl",)


@dataclass
class BenchmarkResult:
    method: str
    group_key: str
    box_count: int
    items_placed: int
    items_unplaced: int
    pallet_count: int
    avg_space_util: float
    runtime_seconds: float
    grid_size: Tuple[int, int, int]
    lookahead: int
    notes: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class CurrentProjectResult:
    group_key: str
    box_count: int
    pallet_count: int
    success_pallets: int
    failed_pallets: int
    avg_fill_rate: float
    avg_mpm_total: float
    runtime_seconds: float

    def to_dict(self) -> Dict:
        return asdict(self)


class ListConveyor:
    """离线箱子列表，模拟 DeepPack3D 的 Conveyor 接口。"""

    def __init__(self, k: int, items: List[Tuple[int, int, int]]):
        self.k = k
        self._source = list(items)
        self._cursor = 0
        self._buffer: List = []
        self.loaded = True

    def reset(self):
        self._cursor = 0
        self._buffer = []
        return self

    def peek(self):
        while len(self._buffer) < self.k and self._cursor < len(self._source):
            self._buffer.append(self._source[self._cursor])
            self._cursor += 1
        while len(self._buffer) < self.k:
            self._buffer.append(None)
        return self._buffer

    def grab(self, n: int = 0):
        assert 0 <= n < self.k
        return self._buffer.pop(n)


def _ensure_reference_on_path() -> None:
    ref = str(REFERENCE_ROOT.resolve())
    if ref not in sys.path:
        sys.path.insert(0, ref)
    if not REFERENCE_ROOT.exists():
        raise FileNotFoundError(
            f"未找到参考项目目录: {REFERENCE_ROOT}"
        )


def scale_boxes_to_grid(
    boxes: List[Dict],
    pallet_dims: Dict[str, float],
    grid_limit: int = 64,
) -> Tuple[List[Tuple[int, int, int]], Tuple[int, int, int]]:
    """把 mm 箱子映射到 DeepPack3D 整数网格 (w, h, d)。

    坐标约定（与参考 env 一致）：
    - w ↔ 当前项目 length（托盘 X）
    - d ↔ 当前项目 width（托盘 Y）
    - h ↔ 当前项目 height（竖直方向）
    """
    length = float(pallet_dims.get("length", 0) or 0)
    width = float(pallet_dims.get("width", 0) or 0)
    height = float(pallet_dims.get("height", 0) or 0)
    if min(length, width, height) <= 0:
        raise ValueError(f"无效托盘尺寸: {pallet_dims}")

    scale = (grid_limit - 1) / max(length, width, height)
    bin_size = (
        max(1, int(round(length * scale))),
        max(1, int(round(height * scale))),
        max(1, int(round(width * scale))),
    )

    scaled: List[Tuple[int, int, int]] = []
    for box in boxes:
        w = max(1, int(round(float(box.get("length", 0) or 0) * scale)))
        h = max(1, int(round(float(box.get("height", 0) or 0) * scale)))
        d = max(1, int(round(float(box.get("width", 0) or 0) * scale)))
        if w > bin_size[0] or h > bin_size[1] or d > bin_size[2]:
            raise ValueError(
                f"箱子 {box.get('id')} 缩放后超出网格: "
                f"{(w, h, d)} vs bin {bin_size}"
            )
        scaled.append((w, h, d))
    return scaled, bin_size


def run_deeppack3d_heuristic(
    method: str,
    boxes: List[Dict],
    pallet_dims: Dict[str, float],
    *,
    lookahead: int = 5,
    grid_limit: int = 64,
) -> BenchmarkResult:
    """运行一种 DeepPack3D 启发式算法。"""
    if method not in HEURISTIC_METHODS:
        raise ValueError(f"不支持的方法: {method}")

    _ensure_reference_on_path()
    from env import MultiBinPackerEnv  # noqa: WPS433

    from benchmark.deeppack3d_heuristics import HEURISTIC_MAP, HeuristicAgent

    heuristics = HEURISTIC_MAP

    scaled_items, bin_size = scale_boxes_to_grid(boxes, pallet_dims, grid_limit)
    group_key = _group_key(boxes)

    env = MultiBinPackerEnv(
        n_bins=1,
        max_bins=-1,
        size=bin_size,
        k=lookahead,
        prealloc_items=0,
        verbose=False,
        replace="all",
    )
    env.conveyor = ListConveyor(k=lookahead, items=scaled_items).reset()

    agent = HeuristicAgent(heuristics[method], env, verbose=False)
    start = time.time()
    placed = 0
    try:
        for step_result in agent.run(max_ep=1, verbose=False):
            if step_result is not None:
                placed += 1
    except Exception as exc:
        runtime = time.time() - start
        return BenchmarkResult(
            method=method,
            group_key=group_key,
            box_count=len(boxes),
            items_placed=placed,
            items_unplaced=max(0, len(boxes) - placed),
            pallet_count=0,
            avg_space_util=0.0,
            runtime_seconds=round(runtime, 4),
            grid_size=bin_size,
            lookahead=lookahead,
            notes=f"运行异常: {exc}",
        )

    runtime = time.time() - start
    utils, used_bins, _ = agent.ep_history[-1] if agent.ep_history else ([], 0, 0)
    avg_util = float(np.mean(utils)) if utils else 0.0

    return BenchmarkResult(
        method=method,
        group_key=group_key,
        box_count=len(boxes),
        items_placed=placed,
        items_unplaced=max(0, len(boxes) - placed),
        pallet_count=int(used_bins),
        avg_space_util=round(avg_util, 6),
        runtime_seconds=round(runtime, 4),
        grid_size=bin_size,
        lookahead=lookahead,
        notes="缩放网格基准；未含指数/吸盘/间隙等业务约束",
    )


def run_current_project_packer(
    boxes: List[Dict],
    pallet_type: str,
    sales_order_no: str,
    target_mpm: Optional[float],
) -> CurrentProjectResult:
    """用当前项目主算法对同一组箱子装箱，便于对比。"""
    if str(CODE_ROOT) not in sys.path:
        sys.path.insert(0, str(CODE_ROOT))

    from src.geometry import validate_center_of_mass
    from src.main.pallet_packer import PalletPacker
    from src.packing import (
        BeamSearchPacker,
        build_centered_single_box_solution,
        build_direct_layer_packing_solution,
    )

    packer = PalletPacker(
        custom_packer_cls=BeamSearchPacker,
        build_direct_layer_solution=build_direct_layer_packing_solution,
        build_centered_single_box_solution=build_centered_single_box_solution,
        validate_center_of_mass=validate_center_of_mass,
    )

    start = time.time()
    type_plan, _, _ = packer.pack_group(
        pallet_type, sales_order_no, boxes, target_mpm
    )
    runtime = time.time() - start

    fills = []
    mpms = []
    for p in type_plan:
        items = p.get("packed_items") or []
        if not items:
            continue
        pd = items[0].get("pallet_dims") or boxes[0].get("pallet_dims") or {}
        pallet_vol = (
            float(pd.get("length", 0) or 0)
            * float(pd.get("width", 0) or 0)
            * float(pd.get("height", 0) or 0)
        )
        box_vol = sum(
            float(it.get("length", 0) or 0)
            * float(it.get("width", 0) or 0)
            * float(it.get("height", 0) or 0)
            for it in items
        )
        fills.append(box_vol / pallet_vol if pallet_vol > 0 else 0.0)
        mpms.append(float(p.get("mpm_total") or 0.0))
    success = sum(1 for p in type_plan if p.get("mpm_status") == "SUCCESS")
    failed = sum(1 for p in type_plan if p.get("mpm_status") == "FAILED")

    return CurrentProjectResult(
        group_key=f"{pallet_type}__{sales_order_no}",
        box_count=len(boxes),
        pallet_count=len(type_plan),
        success_pallets=success,
        failed_pallets=failed,
        avg_fill_rate=round(sum(fills) / max(1, len(fills)), 6),
        avg_mpm_total=round(sum(mpms) / max(1, len(mpms)), 4),
        runtime_seconds=round(runtime, 4),
    )


def run_heuristic_suite(
    boxes: List[Dict],
    pallet_dims: Dict[str, float],
    *,
    lookahead: int = 5,
    grid_limit: int = 64,
    methods: Optional[List[str]] = None,
) -> List[BenchmarkResult]:
    methods = list(methods or HEURISTIC_METHODS)
    results: List[BenchmarkResult] = []
    for method in methods:
        results.append(
            run_deeppack3d_heuristic(
                method,
                boxes,
                pallet_dims,
                lookahead=lookahead,
                grid_limit=grid_limit,
            )
        )
    return results


def _group_key(boxes: List[Dict]) -> str:
    if not boxes:
        return "EMPTY"
    pallet_type = boxes[0].get("pallet_type", "UNKNOWN")
    order = boxes[0].get("sales_order_no", "UNKNOWN")
    return f"{pallet_type}__{order}"


def load_excel_boxes(
    filepath: Optional[Path] = None,
    *,
    limit_groups: int = 1,
    max_boxes_per_group: Optional[int] = 80,
) -> List[Tuple[str, str, List[Dict]]]:
    """从当前项目 Excel 加载并按组返回。"""
    if str(CODE_ROOT) not in sys.path:
        sys.path.insert(0, str(CODE_ROOT))

    from src.data.excel_loader import load_boxes
    from src.main.order_processor import OrderProcessor

    if filepath is None:
        filepath = PROJECT_ROOT / "data" / "多条件筛选随机挑选 5000 个箱子最终结果(单托盘).xlsx"

    boxes = load_boxes(str(filepath))
    if not boxes:
        raise RuntimeError(f"Excel 无数据: {filepath}")

    grouped = OrderProcessor.group_by_order(boxes)
    groups: List[Tuple[str, str, List[Dict]]] = []
    for idx, ((pallet_type, sales_order_no), group_boxes) in enumerate(grouped.items()):
        if idx >= limit_groups:
            break
        if max_boxes_per_group is not None:
            group_boxes = group_boxes[:max_boxes_per_group]
        groups.append((pallet_type, sales_order_no, group_boxes))
    return groups


@dataclass
class ConstrainedResult:
    method: str
    group_key: str
    box_count: int
    items_placed: int
    items_unplaced: int
    pallet_count: int
    success_pallets: int
    failed_pallets: int
    avg_fill_rate: float
    avg_mpm_total: float
    runtime_seconds: float
    constraint_valid: bool = True

    def to_dict(self) -> Dict:
        return asdict(self)


def _summarize_plan(
    type_plan: List[Dict],
    boxes: List[Dict],
    runtime: float,
    method: str,
) -> ConstrainedResult:
    input_ids = {str(b.get("id")) for b in boxes}
    placed_ids = []
    for plan in type_plan:
        for item in plan.get("packed_items", []):
            placed_ids.append(str(item.get("id")))
    placed_set = set(placed_ids)

    fills = []
    mpms = []
    success = failed = 0
    pallet_dims = boxes[0]["pallet_dims"] if boxes else {}
    pallet_vol = (
        float(pallet_dims.get("length", 0) or 0)
        * float(pallet_dims.get("width", 0) or 0)
        * float(pallet_dims.get("height", 0) or 0)
    )
    for plan in type_plan:
        items = plan.get("packed_items") or []
        if not items:
            continue
        if plan.get("mpm_status") == "SUCCESS":
            success += 1
        elif plan.get("mpm_status") == "FAILED":
            failed += 1
        box_vol = sum(
            float(it.get("length", 0) or 0)
            * float(it.get("width", 0) or 0)
            * float(it.get("height", 0) or 0)
            for it in items
        )
        fills.append(box_vol / pallet_vol if pallet_vol > 0 else 0.0)
        mpms.append(float(plan.get("mpm_total") or 0.0))

    return ConstrainedResult(
        method=method,
        group_key=_group_key(boxes),
        box_count=len(boxes),
        items_placed=len(placed_set & input_ids),
        items_unplaced=len(input_ids - placed_set),
        pallet_count=len(type_plan),
        success_pallets=success,
        failed_pallets=failed,
        avg_fill_rate=round(sum(fills) / max(1, len(fills)), 6),
        avg_mpm_total=round(sum(mpms) / max(1, len(mpms)), 4),
        runtime_seconds=round(runtime, 4),
    )


def run_constrained_heuristic(
    method: str,
    boxes: List[Dict],
    target_mpm: Optional[float],
    *,
    lookahead: int = 5,
) -> ConstrainedResult:
    from benchmark.deeppack3d_constrained_packer import DeepPack3DConstrainedPacker

    pallet_type = boxes[0].get("pallet_type", "UNKNOWN")
    sales_order_no = boxes[0].get("sales_order_no", "UNKNOWN")
    packer = DeepPack3DConstrainedPacker(method=method, lookahead=lookahead)
    type_plan, runtime, _ = packer.pack_group(
        pallet_type, sales_order_no, boxes, target_mpm
    )
    return _summarize_plan(
        type_plan,
        boxes,
        runtime.get("packing", 0.0),
        f"c-{method}",
    )


def run_constrained_suite(
    boxes: List[Dict],
    target_mpm: Optional[float],
    *,
    lookahead: int = 5,
    methods: Optional[List[str]] = None,
) -> List[ConstrainedResult]:
    methods = list(methods or HEURISTIC_METHODS)
    return [
        run_constrained_heuristic(method, boxes, target_mpm, lookahead=lookahead)
        for method in methods
    ]


def format_constrained_comparison_table(
    constrained_results: List[ConstrainedResult],
    current_result: Optional[CurrentProjectResult] = None,
) -> str:
    lines = [
        "=" * 96,
        "同台竞技：DeepPack3D 约束版 vs 当前项目（同一 Excel / 同一业务规则）",
        "=" * 96,
        f"{'方法':<10} {'箱数':>6} {'已装':>6} {'未装':>6} {'托盘':>6} "
        f"{'达标盘':>6} {'未达标':>6} {'填充率':>8} {'耗时(s)':>8}",
        "-" * 96,
    ]
    for r in constrained_results:
        lines.append(
            f"{r.method:<10} {r.box_count:>6} {r.items_placed:>6} {r.items_unplaced:>6} "
            f"{r.pallet_count:>6} {r.success_pallets:>6} {r.failed_pallets:>6} "
            f"{r.avg_fill_rate:>8.4f} {r.runtime_seconds:>8.2f}"
        )
    if current_result is not None:
        lines.append("-" * 96)
        lines.append(
            f"{'current':<10} {current_result.box_count:>6} "
            f"{current_result.box_count:>6} {0:>6} "
            f"{current_result.pallet_count:>6} {current_result.success_pallets:>6} "
            f"{current_result.failed_pallets:>6} "
            f"{current_result.avg_fill_rate:>8.4f} {current_result.runtime_seconds:>8.2f}"
        )
    lines.append("=" * 96)
    lines.append(
        "注: 约束版 DeepPack 已启用间隙/支撑/吸盘/叠放/指数封盘；"
        "与 current 使用同一 target_mpm。"
    )
    return "\n".join(lines)


def format_comparison_table(
    deeppack_results: List[BenchmarkResult],
    current_result: Optional[CurrentProjectResult] = None,
) -> str:
    lines = [
        "=" * 88,
        "DeepPack3D 启发式 vs 当前项目（同一 Excel 分组）",
        "=" * 88,
        f"{'方法':<8} {'箱数':>6} {'已装':>6} {'未装':>6} {'托盘数':>6} "
        f"{'空间利用率':>10} {'耗时(s)':>8}",
        "-" * 88,
    ]
    for r in deeppack_results:
        lines.append(
            f"{r.method:<8} {r.box_count:>6} {r.items_placed:>6} {r.items_unplaced:>6} "
            f"{r.pallet_count:>6} {r.avg_space_util:>10.4f} {r.runtime_seconds:>8.2f}"
        )
    if current_result is not None:
        lines.append("-" * 88)
        lines.append(
            f"{'current':<8} {current_result.box_count:>6} "
            f"{current_result.box_count:>6} {0:>6} "
            f"{current_result.pallet_count:>6} "
            f"{current_result.avg_fill_rate:>10.4f} {current_result.runtime_seconds:>8.2f}"
        )
        lines.append(
            f"  指数达标盘: {current_result.success_pallets}  "
            f"未达标: {current_result.failed_pallets}  "
            f"平均指数: {current_result.avg_mpm_total}"
        )
    lines.append("=" * 88)
    lines.append("注: DeepPack3D 列为缩放网格下的空间利用率；current 列为业务算法填充率。")
    return "\n".join(lines)
