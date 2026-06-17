"""
数据加载模块

加载 Excel 数据并预处理为装箱算法可用的字典列表。
"""

from .excel_loader import load_boxes
from .api_loader import (
    load_boxes_from_api,
    fetch_and_save_stock_json,
    load_boxes_from_local_json,
)
from .wcs_output import build_wcs_pallet_plan_payload, send_pallet_plan_result

__all__ = [
    "load_boxes",
    "load_boxes_from_api",
    "fetch_and_save_stock_json",
    "load_boxes_from_local_json",
    "build_wcs_pallet_plan_payload",
    "send_pallet_plan_result",
]
