"""
把 run_constrained_benchmark 的终端/txt 输出转成 Excel（无需重跑 benchmark）。

用法:
    python benchmark/export_constrained_txt_to_excel.py
    python benchmark/export_constrained_txt_to_excel.py ../output/constrained_full_5000.txt
    python benchmark/export_constrained_txt_to_excel.py ../output/constrained_full_5000.txt -o ../output/对比结果.xlsx
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from benchmark.deeppack3d_benchmark import (  # noqa: E402
    PROJECT_ROOT,
    build_constrained_summary_rows,
    export_constrained_benchmark_excel,
)

GROUP_RE = re.compile(
    r"分组:\s*(\S+)\s*/\s*(\S+)，箱数:\s*(\d+)"
)
TARGET_RE = re.compile(r"目标指数 target_mpm = (\S+)")
ROW_RE = re.compile(
    r"^(c-\w+|current)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s*$"
)


def _read_text_auto(path: Path) -> str:
  raw = path.read_bytes()
  if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
    return raw.decode("utf-16")
  if raw.startswith(b"\xef\xbb\xbf"):
    return raw.decode("utf-8-sig")
  return raw.decode("utf-8")


def parse_constrained_txt(text: str) -> list[dict]:
  rows: list[dict] = []
  pallet_type = ""
  sales_order_no = ""
  target_mpm = None

  for line in text.splitlines():
    line = line.strip()
    group_match = GROUP_RE.search(line)
    if group_match:
      pallet_type, sales_order_no, _box_count = group_match.groups()
      continue

    target_match = TARGET_RE.search(line)
    if target_match:
      raw = target_match.group(1)
      target_mpm = None if raw == "None" else float(raw)
      continue

    row_match = ROW_RE.match(line)
    if not row_match or not pallet_type:
      continue

    (
      method,
      box_count,
      placed,
      unplaced,
      pallet_count,
      success,
      failed,
      fill_rate,
      runtime,
    ) = row_match.groups()
    box_count_i = int(box_count)
    placed_i = int(placed)
    rows.append({
      "托盘类型": pallet_type,
      "销售订单号": sales_order_no,
      "目标指数": target_mpm,
      "方法": method,
      "箱数": box_count_i,
      "已装": placed_i,
      "未装": int(unplaced),
      "装完率": round(placed_i / box_count_i, 6) if box_count_i else 0.0,
      "托盘数": int(pallet_count),
      "达标盘": int(success),
      "未达标盘": int(failed),
      "平均填充率": float(fill_rate),
      "平均指数": None,
      "耗时秒": float(runtime),
    })

  return rows


def main() -> None:
  parser = argparse.ArgumentParser(
    description="将约束版 benchmark txt 输出导出为 Excel"
  )
  parser.add_argument(
    "input_txt",
    nargs="?",
    type=Path,
    default=PROJECT_ROOT / "output" / "constrained_full_5000.txt",
  )
  parser.add_argument(
    "-o",
    "--output",
    type=Path,
    default=None,
    help="输出 xlsx 路径，默认 output/constrained_benchmark_时间戳.xlsx",
  )
  args = parser.parse_args()

  input_path = Path(args.input_txt)
  if not input_path.exists():
    raise FileNotFoundError(f"找不到输入文件: {input_path}")

  detail_rows = parse_constrained_txt(_read_text_auto(input_path))
  if not detail_rows:
    raise RuntimeError(f"未能从 {input_path} 解析到任何结果行")

  if args.output is None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = PROJECT_ROOT / "output" / f"constrained_benchmark_{stamp}.xlsx"
  else:
    output_path = Path(args.output)

  summary_rows = build_constrained_summary_rows(detail_rows)
  export_constrained_benchmark_excel(
    detail_rows, output_path, summary_rows=summary_rows
  )
  print(f"已导出 {len(detail_rows)} 行明细 -> {output_path.resolve()}")
  print("工作表: 分组明细 / 汇总")


if __name__ == "__main__":
  main()
