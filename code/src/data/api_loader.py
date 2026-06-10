"""
HTTP API 数据加载器

从 WCS Mock 接口获取库存数据，转换为与 excel_loader.load_boxes 完全相同的
箱子字典列表，供装箱算法使用。

替换关系：
    excel_loader.load_boxes  →  api_loader.load_boxes_from_api
    两者返回的 List[Dict] 结构完全一致，装箱核心逻辑无需任何改动。
"""

import os
import time
import uuid
from typing import Dict, List, Optional

import requests
import urllib3
import pandas as pd
import numpy as np
from .excel_loader import _detect_small_box_threshold
from src.config.constants import SMALL_BOX_SOURCE_FILE, SMALL_BOX_BMS_SHEET
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Mock Server 地址，可通过环境变量覆盖
DEFAULT_BASE_URL = os.getenv(
    "WCS_MOCK_URL",
    "https://3c3758c8-755a-499e-b580-76afda706e5e.mock.pstmn.io",
)

# Load BMS sheet once to get min_pack_multiple per box_type (used by API loader)
try:
    _BMS_DF = pd.read_excel(SMALL_BOX_SOURCE_FILE, sheet_name=SMALL_BOX_BMS_SHEET)
    _BMS_DF = _BMS_DF.set_index('包装规格代码')
except Exception as e:
    print(f"警告：读取 BMS 表失败，min_pack_multiple 将使用默认值 0. 错误: {e}")
    _BMS_DF = pd.DataFrame()


def _make_msg_header() -> Dict[str, str]:
    """生成接口1所需的请求头字段（msgtime + msgid）。"""
    return {
        "msgtime": time.strftime("%Y年%m月%d日%H:%M:%S"),
        "msgid": uuid.uuid4().hex,
    }


def _fetch_stock(base_url: str) -> List[Dict]:
    """
    调用接口1（/adaptor/api/wcs/reqstockinfo）获取库存信息。

    返回原始的库存条目列表（每条代表一种箱子，含 target_num 表示数量）。
    """
    url = f"{base_url.rstrip('/')}/adaptor/api/wcs/reqstockinfo"
    resp = requests.post(url, json=_make_msg_header(), timeout=30, verify=False)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(
            f"接口1返回错误: code={body.get('code')}, msg={body.get('msg')}"
        )
    return body.get("data", [])


def _fetch_pallet_dims(base_url: str, case_type: str) -> Dict[str, float]:
    """
    调用接口6（/adaptor/api/wcs/palletarrive）获取托盘尺寸。

    Returns:
        {"length": float, "width": float, "height": float}
    """
    url = f"{base_url.rstrip('/')}/adaptor/api/wcs/palletarrive"
    payload = {
        "robot_id": "001",
        "station_id": "001",
        "pallet_code": "",
        "case_type": case_type,
    }
    resp = requests.post(url, json=payload, timeout=30, verify=False)
    resp.raise_for_status()
    body = resp.json()
    dims = (body.get("data") or {}).get("pallet_dims", {})
    return {
        "length": float(dims.get("length", 0) or 0),
        "width": float(dims.get("width", 0) or 0),
        "height": float(dims.get("height", 0) or 0),
    }


def _expand_stock_to_boxes(
    stock_entries: List[Dict],
    pallet_dims_map: Dict[str, Dict[str, float]],
) -> List[Dict]:
    """
    将库存条目（每条含 target_num）展开为独立的箱子字典列表。

    与 excel_loader.load_boxes 返回的结构保持一致。
    """
    boxes: List[Dict] = []
    for entry in stock_entries:
        box_type = entry.get("box_type", "UNKNOWN")
        case_type = entry.get("case_type", "MH423C")
        order_id = entry.get("order_id", "UNKNOWN_ORDER")
        target_num = int(entry.get("target_num", 0) or 0)

        length = float(entry.get("length", 0) or 0)
        width = float(entry.get("width", 0) or 0)
        height = float(entry.get("height", 0) or 0)
        weight = float(entry.get("weight", 0) or 0)

        dims = pallet_dims_map.get(case_type, {})

        # 从 BMS 表获取每种箱型对应的最小包装倍数，若未找到则默认 0
        if not _BMS_DF.empty and box_type in _BMS_DF.index:
            min_pack_multiple = float(_BMS_DF.loc[box_type, '最小包装量的倍数'])
        else:
            min_pack_multiple = 0.0

        for i in range(target_num):
            box_id = f"{order_id}_{box_type}-{i + 1}"
            boxes.append({
                "id": box_id,
                "original_box_id": box_id,
                "type": box_type,
                "length": length,
                "width": width,
                "height": height,
                "weight": weight,
                "min_pack_multiple": min_pack_multiple,
                "pallet_type": case_type,
                "sales_order_no": str(order_id),
                "pallet_dims": dict(dims),  # 每个箱子都带一份托盘尺寸
                "is_small_box": False,
                "volume": length * width * height,
                "包装规格代码": str(box_type),
            })

    return boxes


def load_boxes_from_api(
    filepath: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Optional[List[Dict]]:
    """
    从 WCS HTTP 接口加载箱子数据。

    该函数的签名和返回值与 excel_loader.load_boxes 保持一致，
    可以直接替换 PackingWorkflow 的 preprocess_fn。

    Args:
        filepath: 保留参数（为了与 load_boxes 签名兼容），本函数不使用。
        base_url: Mock Server 地址；None 时使用环境变量或默认值。

    Returns:
        箱子字典列表，结构与 excel_loader.load_boxes 完全相同。
        请求失败时返回 None。
    """
    if base_url is None:
        base_url = DEFAULT_BASE_URL

    try:
        # 1. 获取库存
        print(f"正在从 WCS 接口获取库存数据 ({base_url}) ...")
        stock_entries = _fetch_stock(base_url)
        print(f"  获取到 {len(stock_entries)} 种箱型。")

        # 2. 按 case_type 获取托盘尺寸（去重，避免重复请求）
        case_types = {
            entry.get("case_type", "MH423C") for entry in stock_entries
        }
        pallet_dims_map: Dict[str, Dict[str, float]] = {}
        for ct in case_types:
            print(f"  获取托盘尺寸: case_type={ct} ...")
            pallet_dims_map[ct] = _fetch_pallet_dims(base_url, ct)
            print(f"    → {pallet_dims_map[ct]}")

        # 3. 展开为独立箱子记录
        all_boxes = _expand_stock_to_boxes(stock_entries, pallet_dims_map)
        total = len(all_boxes)
        print(f"  共展开为 {total} 个箱子记录。")

        # ---------- 开始小箱子判定逻辑（与 excel_loader 相同） ----------
        df_boxes = pd.DataFrame(all_boxes)
        # 体积 (mm^3) 与体积 (m^3)
        df_boxes['体积(mm^3)'] = df_boxes['length'] * df_boxes['width'] * df_boxes['height']
        df_boxes['体积(m^3)'] = df_boxes['体积(mm^3)'] / 1_000_000_000.0
        # 密度与密度/体积指数
        df_boxes['密度(kg/m^3)'] = df_boxes['weight'] / df_boxes['体积(m^3)']
        df_boxes['密度/体积指数'] = df_boxes['密度(kg/m^3)'] / df_boxes['体积(m^3)']

        # 检测阈值
        threshold_volume = _detect_small_box_threshold(
            df_boxes[['包装规格代码', '体积(mm^3)', '密度/体积指数']]
        )
        if threshold_volume is None:
            threshold_volume = float('inf')
            df_boxes['is_small_box'] = False
        else:
            df_boxes['is_small_box'] = df_boxes['体积(mm^3)'] < threshold_volume - 1e-9

        # 统计并打印
        small_box_count = int(df_boxes['is_small_box'].sum())
        non_small_box_count = int((~df_boxes['is_small_box']).sum())
        threshold_text = (
            '未能检测到有效阈值' if not np.isfinite(threshold_volume)
            else f'{threshold_volume:.2f} mm^3'
        )
        print(f"检测到小箱子体积阈值: {threshold_text}")
        print(f"小箱子数量: {small_box_count}，非小箱子数量: {non_small_box_count}")

        # 去除中间计算列，保留业务字段
        all_boxes = df_boxes.drop(
            columns=['体积(mm^3)', '体积(m^3)', '密度(kg/m^3)', '密度/体积指数'],
            errors='ignore',
        ).to_dict('records')
        # 确保每个箱子都有必备字段（防止意外缺失）
        for box in all_boxes:
            box.setdefault('is_small_box', False)
            box.setdefault('volume', box['length'] * box['width'] * box['height'])
            box.setdefault('weight', float(box.get('weight', 0) or 0))
        # ---------- 结束小箱子判定逻辑 ----------

        if not all_boxes:
            print("警告：接口返回的库存数据为空。")
            return None

        return all_boxes

    except requests.RequestException as exc:
        print(f"错误：请求 WCS 接口失败: {exc}")
        return None
    except Exception as exc:
        print(f"错误：加载 API 数据时发生异常: {exc}")
        return None
