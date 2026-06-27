"""
DeepPack3D 参考算法对比脚本

用法（在 code/ 目录下）:
    python benchmark/run_deeppack3d_benchmark.py
    python benchmark/run_deeppack3d_benchmark.py --max-boxes 50 --lookahead 5
    python benchmark/run_deeppack3d_benchmark.py --skip-current
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
    format_comparison_table,
    load_excel_boxes,
    run_current_project_packer,
    run_heuristic_suite,
)
from src.config.constants import PALLET_INDEX_TARGETS  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="DeepPack3D 启发式 vs 当前装箱算法")
    parser.add_argument(
        "--excel",
        type=Path,
        default=None,
        help="Excel 路径，默认 data/多条件筛选随机挑选 5000 个箱子最终结果(单托盘).xlsx",
    )
    parser.add_argument("--max-boxes", type=int, default=60, help="每组最多取多少箱")
    parser.add_argument("--lookahead", type=int, default=5, help="DeepPack3D lookahead")
    parser.add_argument("--grid-limit", type=int, default=64, help="缩放网格上限")
    parser.add_argument("--groups", type=int, default=1, help="对比前 N 个订单分组")
    parser.add_argument(
        "--methods",
        nargs="+",
        default=list(HEURISTIC_METHODS),
        choices=list(HEURISTIC_METHODS),
        help="要运行的 DeepPack3D 启发式",
    )
    parser.add_argument(
        "--skip-current",
        action="store_true",
        help="只跑 DeepPack3D，不跑当前项目算法",
    )
    args = parser.parse_args()

    groups = load_excel_boxes(
        args.excel,
        limit_groups=args.groups,
        max_boxes_per_group=args.max_boxes,
    )

    for pallet_type, sales_order_no, boxes in groups:
        print(f"\n分组: {pallet_type} / {sales_order_no}，箱数: {len(boxes)}")
        pallet_dims = boxes[0]["pallet_dims"]

        deeppack_results = run_heuristic_suite(
            boxes,
            pallet_dims,
            lookahead=args.lookahead,
            grid_limit=args.grid_limit,
            methods=args.methods,
        )

        current_result = None
        if not args.skip_current:
            target_mpm = PALLET_INDEX_TARGETS.get(pallet_type)
            print(f"运行当前项目算法 (target_mpm={target_mpm}) ...")
            current_result = run_current_project_packer(
                boxes,
                pallet_type,
                sales_order_no,
                target_mpm,
            )

        print(format_comparison_table(deeppack_results, current_result))


if __name__ == "__main__":
    main()
