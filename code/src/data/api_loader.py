"""
HTTP API 数据加载器

从 WCS Mock 接口获取库存数据，转换为与 excel_loader.load_boxes 完全相同的
箱子字典列表，供装箱算法使用。

替换关系：
    excel_loader.load_boxes  →  api_loader.load_boxes_from_api
    两者返回的 List[Dict] 结构完全一致，装箱核心逻辑无需任何改动。
"""

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
import urllib3
import pandas as pd
import numpy as np
from .excel_loader import _detect_small_box_threshold
from src.config.constants import (
    SMALL_BOX_SOURCE_FILE, SMALL_BOX_BMS_SHEET, SMALL_BOX_SOURCE_SHEET,
)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Mock Server 地址，可通过环境变量覆盖
DEFAULT_BASE_URL = os.getenv(
    "WCS_MOCK_URL",
    "https://3c3758c8-755a-499e-b580-76afda706e5e.mock.pstmn.io",
)

# ============================================================================
# 从 Excel 一次性加载两张表：
#   1. BMS 表 → box_type 对应的 min_pack_multiple
#   2. 数据表 → case_type 对应的 pallet_dims（托盘尺寸）
# ============================================================================
_BMS_DF = pd.DataFrame()
_PALLET_DIMS_MAP: Dict[str, Dict[str, float]] = {}

try:
    _BMS_DF = pd.read_excel(SMALL_BOX_SOURCE_FILE, sheet_name=SMALL_BOX_BMS_SHEET)
    _BMS_DF = _BMS_DF.set_index('包装规格代码')
except Exception as e:
    print(f"警告：读取 BMS 表失败，min_pack_multiple 将使用默认值 0. 错误: {e}")

try:
    _excel = pd.ExcelFile(SMALL_BOX_SOURCE_FILE)
    _source_sheet = SMALL_BOX_SOURCE_SHEET
    if _source_sheet not in _excel.sheet_names:
        for _sn in _excel.sheet_names:
            if _sn not in {SMALL_BOX_BMS_SHEET, "说明"}:
                _source_sheet = _sn
                break
    _df_tasks = pd.read_excel(SMALL_BOX_SOURCE_FILE, sheet_name=_source_sheet)
    # 按 Case类型 去重，取第一条的托盘长/宽/高
    for _, _row in _df_tasks.drop_duplicates(subset=['Case类型']).iterrows():
        _ct = str(_row['Case类型'])
        _PALLET_DIMS_MAP[_ct] = {
            "length": float(_row.get('托盘长', 0) or 0),
            "width": float(_row.get('托盘宽', 0) or 0),
            "height": float(_row.get('托盘高', 0) or 0),
        }
    print(f"从 Excel 加载托盘尺寸映射：{_PALLET_DIMS_MAP}")
except Exception as e:
    print(f"警告：读取 Excel 托盘尺寸失败: {e}")


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


def _get_pallet_dims_from_excel(case_type: str) -> Dict[str, float]:
    """
    从 Excel 预加载的映射表中查找托盘尺寸。

    Returns:
        {"length": float, "width": float, "height": float}
    """
    dims = _PALLET_DIMS_MAP.get(case_type, {})
    if not dims:
        print(f"  警告：Excel 中未找到 case_type={case_type} 的托盘尺寸，使用空值。")
    return dims


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

        # 2. 从 Excel 查找托盘尺寸（按 case_type）
        case_types = {
            entry.get("case_type", "MH423C") for entry in stock_entries
        }
        pallet_dims_map: Dict[str, Dict[str, float]] = {}
        for ct in case_types:
            pallet_dims_map[ct] = _get_pallet_dims_from_excel(ct)
            print(f"  托盘尺寸 (来自Excel): case_type={ct} → {pallet_dims_map[ct]}")

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


# ============================================================================
# 定时下载 + 本地文件加载（生产者/消费者模式专用）
# ============================================================================

def fetch_and_save_stock_json(
    input_dir: Path,
    base_url: Optional[str] = None,
) -> Optional[Path]:
    """
    向 WCS 接口发送一次请求，把返回的原始 JSON 保存到 input_dir。

    文件名格式：YYYYMMDD_HHMMSS.json（按时间自然排序）。

    Returns:
        保存成功时返回文件路径；失败时返回 None。
    """
    if base_url is None:
        base_url = DEFAULT_BASE_URL

    try:
        url = f"{base_url.rstrip('/')}/adaptor/api/wcs/reqstockinfo"
        resp = requests.post(url, json=_make_msg_header(), timeout=30, verify=False)
        resp.raise_for_status()

        # 确保目录存在
        input_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = input_dir / f"{ts}.json"
        filepath.write_text(resp.text, encoding="utf-8")
        print(f"[下载] {ts} → 已保存 {filepath.name}")
        return filepath

    except Exception as exc:
        print(f"[下载] 错误: {exc}")
        return None


def load_boxes_from_local_json(
    filepath: str,
) -> Optional[List[Dict]]:
    """
    从本地已保存的库存 JSON 文件中加载箱子数据。

    该函数的处理逻辑（展开、托盘尺寸、小箱子判定）与 load_boxes_from_api
    完全一致，唯一区别是数据来源从 HTTP 接口变为本地 JSON 文件。

    Args:
        filepath: 本地 JSON 文件路径。

    Returns:
        箱子字典列表；文件异常时返回 None。
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            body = json.load(f)

        if body.get("code") != 0:
            print(f"[加载] JSON 内容错误: code={body.get('code')}, msg={body.get('msg')}")
            return None

        stock_entries = body.get("data", [])
        print(f"  从文件 {Path(filepath).name} 读取到 {len(stock_entries)} 种箱型。")

        # 托盘尺寸从 Excel 映射获取
        case_types = {
            entry.get("case_type", "MH423C") for entry in stock_entries
        }
        pallet_dims_map: Dict[str, Dict[str, float]] = {}
        for ct in case_types:
            pallet_dims_map[ct] = _get_pallet_dims_from_excel(ct)

        # 展开为独立箱子记录
        all_boxes = _expand_stock_to_boxes(stock_entries, pallet_dims_map)
        total = len(all_boxes)
        print(f"  共展开为 {total} 个箱子记录。")

        # ---------- 小箱子判定逻辑（与 load_boxes_from_api 完全相同） ----------
        df_boxes = pd.DataFrame(all_boxes)
        df_boxes['体积(mm^3)'] = df_boxes['length'] * df_boxes['width'] * df_boxes['height']
        df_boxes['体积(m^3)'] = df_boxes['体积(mm^3)'] / 1_000_000_000.0
        df_boxes['密度(kg/m^3)'] = df_boxes['weight'] / df_boxes['体积(m^3)']
        df_boxes['密度/体积指数'] = df_boxes['密度(kg/m^3)'] / df_boxes['体积(m^3)']

        threshold_volume = _detect_small_box_threshold(
            df_boxes[['包装规格代码', '体积(mm^3)', '密度/体积指数']]
        )
        if threshold_volume is None:
            threshold_volume = float('inf')
            df_boxes['is_small_box'] = False
        else:
            df_boxes['is_small_box'] = df_boxes['体积(mm^3)'] < threshold_volume - 1e-9

        small_box_count = int(df_boxes['is_small_box'].sum())
        non_small_box_count = int((~df_boxes['is_small_box']).sum())
        threshold_text = (
            '未能检测到有效阈值' if not np.isfinite(threshold_volume)
            else f'{threshold_volume:.2f} mm^3'
        )
        print(f"  小箱子阈值: {threshold_text}，小箱: {small_box_count}，非小箱: {non_small_box_count}")

        all_boxes = df_boxes.drop(
            columns=['体积(mm^3)', '体积(m^3)', '密度(kg/m^3)', '密度/体积指数'],
            errors='ignore',
        ).to_dict('records')
        for box in all_boxes:
            box.setdefault('is_small_box', False)
            box.setdefault('volume', box['length'] * box['width'] * box['height'])
            box.setdefault('weight', float(box.get('weight', 0) or 0))
        # ---------- 结束小箱子判定逻辑 ----------

        if not all_boxes:
            print("  警告：文件中的库存数据为空。")
            return None

        return all_boxes

    except Exception as exc:
        print(f"[加载] 读取文件 {filepath} 时发生异常: {exc}")
        return None

