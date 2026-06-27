"""
DeepPack3D 约束版同台竞技脚本

在同一 Excel 数据、同一业务规则（指数目标 + 几何/吸盘约束）下，
对比 DeepPack3D 四种启发式与当前项目算法。

用法:
    python benchmark/run_constrained_benchmark.py
    python benchmark/run_constrained_benchmark.py --max-boxes 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from benchmark.deeppack3d_benchmark import (  # noqa: E402
    HEURISTIC_METHODS,
    format_constrained_comparison_table,
    load_excel_boxes,
    run_current_project_packer,
)
from src.config.constants import PALLET_INDEX_TARGETS  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DeepPack3D 约束版 vs 当前项目（同台竞技）"
    )
    parser.add_argument("--excel", type=Path, default=None)
    parser.add_argument("--max-boxes", type=int, default=40)
    parser.add_argument("--groups", type=int, default=1)
    parser.add_argument("--lookahead", type=int, default=5)
    parser.add_argument(
        "--methods",
        nargs="+",
        default=list(HEURISTIC_METHODS),
        choices=list(HEURISTIC_METHODS),
    )
    parser.add_argument("--skip-current", action="store_true")
    args = parser.parse_args()

    groups = load_excel_boxes(
        args.excel,
        limit_groups=args.groups,
        max_boxes_per_group=args.max_boxes,
    )

    for pallet_type, sales_order_no, boxes in groups:
        target_mpm = PALLET_INDEX_TARGETS.get(pallet_type)
        print(f"\n分组: {pallet_type} / {sales_order_no}，箱数: {len(boxes)}")
        print(f"目标指数 target_mpm = {target_mpm}", flush=True)

        constrained = []
        for method in args.methods:
            print(f"  运行 c-{method} ...", flush=True)
            from benchmark.deeppack3d_benchmark import run_constrained_heuristic

            result = run_constrained_heuristic(
                method, boxes, target_mpm, lookahead=args.lookahead
            )
            constrained.append(result)
            print(
                f"  c-{method} 完成: 已装 {result.items_placed}, "
                f"托盘 {result.pallet_count}, 耗时 {result.runtime_seconds:.1f}s",
                flush=True,
            )

        current = None
        if not args.skip_current:
            print("运行当前项目算法 ...", flush=True)
            current = run_current_project_packer(
                boxes, pallet_type, sales_order_no, target_mpm
            )
            print(
                f"  current 完成: 托盘 {current.pallet_count}, "
                f"耗时 {current.runtime_seconds:.1f}s",
                flush=True,
            )

        print(format_constrained_comparison_table(constrained, current), flush=True)


if __name__ == "__main__":
    main()
