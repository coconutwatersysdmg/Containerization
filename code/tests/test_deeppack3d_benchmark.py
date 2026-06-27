"""
DeepPack3D 参考算法基准测试

需要参考项目存在于:
  ../参考/SIMPAC-2024-311-main

若目录不存在或依赖缺失，测试会自动 skip。
"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from benchmark.deeppack3d_benchmark import (  # noqa: E402
    REFERENCE_ROOT,
    format_comparison_table,
    load_excel_boxes,
    run_constrained_heuristic,
    run_constrained_suite,
    run_current_project_packer,
    run_deeppack3d_heuristic,
    scale_boxes_to_grid,
)
from src.config.constants import PALLET_INDEX_TARGETS  # noqa: E402

REFERENCE_AVAILABLE = REFERENCE_ROOT.exists()
EXCEL_PATH = project_root.parent / "data" / "多条件筛选随机挑选 5000 个箱子最终结果(单托盘).xlsx"
EXCEL_AVAILABLE = EXCEL_PATH.exists()


pytestmark = pytest.mark.skipif(
    not REFERENCE_AVAILABLE,
    reason=f"参考项目不存在: {REFERENCE_ROOT}",
)


def _sample_boxes(n: int = 12):
    pallet_dims = {"length": 1440.0, "width": 2240.0, "height": 720.0}
    boxes = []
    for i in range(n):
        boxes.append({
            "id": f"BOX-{i}",
            "type": "TEST",
            "length": 350.0,
            "width": 265.0,
            "height": 120.0 if i % 2 == 0 else 240.0,
            "weight": 1.0,
            "min_pack_multiple": 1.0,
            "pallet_type": "MH423C",
            "sales_order_no": "TEST_ORDER",
            "pallet_dims": dict(pallet_dims),
        })
    return boxes, pallet_dims


def test_scale_boxes_to_grid():
    boxes, pallet_dims = _sample_boxes(3)
    scaled, bin_size = scale_boxes_to_grid(boxes, pallet_dims, grid_limit=64)
    assert len(scaled) == 3
    assert all(min(item) >= 1 for item in scaled)
    assert all(max(item) <= max(bin_size) for item in scaled)


@pytest.mark.parametrize("method", ["bl", "baf", "bssf", "blsf"])
def test_deeppack3d_heuristic_smoke(method):
    boxes, pallet_dims = _sample_boxes(20)
    result = run_deeppack3d_heuristic(
        method,
        boxes,
        pallet_dims,
        lookahead=5,
        grid_limit=64,
    )
    assert result.method == method
    assert result.box_count == 20
    assert result.items_placed > 0
    assert result.pallet_count >= 1
    assert result.runtime_seconds >= 0


def test_all_four_heuristics_on_toy_data():
    boxes, pallet_dims = _sample_boxes(30)
    rows = []
    for method in ("bl", "baf", "bssf", "blsf"):
        rows.append(run_deeppack3d_heuristic(method, boxes, pallet_dims))
    table = format_comparison_table(rows)
    assert "bl" in table and "baf" in table


@pytest.mark.skipif(not EXCEL_AVAILABLE, reason="Excel 数据文件不存在")
def test_constrained_bl_on_excel_subset():
    groups = load_excel_boxes(EXCEL_PATH, limit_groups=1, max_boxes_per_group=25)
    pallet_type, sales_order_no, boxes = groups[0]
    target = PALLET_INDEX_TARGETS.get(pallet_type)
    result = run_constrained_heuristic("bl", boxes, target, lookahead=5)
    assert result.items_placed > 0
    assert result.pallet_count >= 1
    assert result.method == "c-bl"


def test_constrained_suite_smoke():
    boxes, pallet_dims = _sample_boxes(15)
    target = 15.0
    results = run_constrained_suite(boxes, target, lookahead=3)
    assert len(results) == 4
    assert all(r.items_placed > 0 for r in results)


@pytest.mark.skipif(not EXCEL_AVAILABLE, reason="Excel 数据文件不存在")
def test_excel_first_group_benchmark():
    groups = load_excel_boxes(EXCEL_PATH, limit_groups=1, max_boxes_per_group=40)
    assert len(groups) == 1
    pallet_type, sales_order_no, boxes = groups[0]
    assert len(boxes) <= 40

    bl = run_deeppack3d_heuristic("bl", boxes, boxes[0]["pallet_dims"], grid_limit=64)
    assert bl.items_placed > 0

    target = PALLET_INDEX_TARGETS.get(pallet_type)
    current = run_current_project_packer(boxes, pallet_type, sales_order_no, target)
    table = format_comparison_table([bl], current)
    assert "current" in table
    assert current.pallet_count >= 1
