"""
WCS 装箱结果输出

将算法 report 转换为 /adaptor/api/wcs/sendpalletplanresult 接口所需格式并发送。
"""

import uuid
from typing import Any, Dict, List, Optional

import requests
import urllib3

from .api_loader import DEFAULT_BASE_URL
from src.utils.helpers import placement_sort_key

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _parse_product_code(value: Any) -> int:
    """将 product_code 转为接口要求的 int。"""
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _parse_case_group_num(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _item_dims(item: Dict) -> Dict[str, float]:
    """取箱子原始尺寸（mm）。"""
    length = item.get("original_length", item.get("raw_length", item.get("length", 0)))
    width = item.get("original_width", item.get("raw_width", item.get("width", 0)))
    height = item.get("original_height", item.get("raw_height", item.get("height", 0)))
    return {
        "length": float(length or 0),
        "width": float(width or 0),
        "height": float(height or 0),
    }


def _group_items_into_layers(items: List[Dict]) -> List[List[Dict]]:
    """按 z 坐标分层，同层内按放置顺序排序。"""
    if not items:
        return []

    sorted_items = sorted(items, key=placement_sort_key)
    layers: List[List[Dict]] = []
    current_z: Optional[float] = None
    current_layer: List[Dict] = []

    for item in sorted_items:
        z = float((item.get("position") or {}).get("z", 0) or 0)
        if current_z is None or abs(z - current_z) > 1e-6:
            if current_layer:
                layers.append(current_layer)
            current_layer = [item]
            current_z = z
        else:
            current_layer.append(item)

    if current_layer:
        layers.append(current_layer)

    return layers


def _pallet_total_height(pallet: Dict, items: List[Dict]) -> float:
    pallet_dims = {}
    if items:
        pallet_dims = items[0].get("pallet_dims") or {}
    pallet_height = float(pallet_dims.get("height", 0) or 0)
    if pallet_height > 0:
        return pallet_height

    max_top = 0.0
    for item in items:
        pos = item.get("position") or {}
        dims = _item_dims(item)
        top = float(pos.get("z", 0) or 0) + dims["height"]
        max_top = max(max_top, top)
    return max_top


def _build_carton(item: Dict, layer_id: int, seq: int) -> Dict:
    dims = _item_dims(item)
    return {
        "length": dims["length"],
        "width": dims["width"],
        "height": dims["height"],
        "layer_id": layer_id,
        "seq": seq,
        "product_code": _parse_product_code(item.get("product_code")),
    }


def build_wcs_pallet_plan_payload(report: Dict) -> List[Dict]:
    """将算法 report 转为 WCS sendpalletplanresult 接口格式。

    只包含拼 case 成功的托盘（有箱子且 mpm_status 为 SUCCESS）。
    若无成功托盘则返回 []。
    """
    pallets = report.get("pallets") or []
    result: List[Dict] = []
    box_index = 0

    for pallet in pallets:
        items = pallet.get("packed_items") or []
        if not items:
            continue
        if pallet.get("mpm_status") != "SUCCESS":
            continue

        box_index += 1
        first_item = items[0]
        layer_groups = _group_items_into_layers(items)

        layers = []
        seq = 0
        for layer_idx, layer_items in enumerate(layer_groups, start=1):
            cartons = []
            for item in layer_items:
                seq += 1
                cartons.append(_build_carton(item, layer_idx, seq))
            layers.append({"cartons": cartons})

        result.append({
            "box_index": box_index,
            "box_unique_id": uuid.uuid4().hex,
            "total_height": _pallet_total_height(pallet, items),
            "order_id": str(
                pallet.get("sales_order_no")
                or first_item.get("sales_order_no")
                or ""
            ),
            "case_group_num": _parse_case_group_num(first_item.get("case_group")),
            "case_type": str(
                pallet.get("pallet_type")
                or first_item.get("pallet_type")
                or "MH423C"
            ),
            "layers": layers,
        })

    return result


def send_pallet_plan_result(
    payload: List[Dict],
    base_url: Optional[str] = None,
) -> bool:
    """向 WCS 发送拼 case 结果。

    Args:
        payload: build_wcs_pallet_plan_payload 生成的数组，可为 []。
        base_url: WCS 服务地址，默认读取环境变量 WCS_MOCK_URL。

    Returns:
        发送成功返回 True，失败返回 False。
    """
    if base_url is None:
        base_url = DEFAULT_BASE_URL

    url = f"{base_url.rstrip('/')}/adaptor/api/wcs/sendpalletplanresult"
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
            verify=False,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            print(
                f"[上传] 接口返回错误: code={body.get('code')}, "
                f"msg={body.get('msg')}"
            )
            return False
        print(f"[上传] 拼 case 结果已发送，共 {len(payload)} 个 case")
        return True
    except requests.RequestException as exc:
        print(f"[上传] 请求失败: {exc}")
        return False
    except Exception as exc:
        print(f"[上传] 发送异常: {exc}")
        return False
