"""
支撑面积计算函数

提供箱子支撑面积和支撑比例的计算功能。
"""

from typing import Dict, List
from shapely.geometry import box as shapely_box
from shapely.ops import unary_union


def calculate_direct_supported_area(
    point: Dict[str, float],
    dims: Dict[str, float],
    placed_boxes: List[Dict]
) -> float:
    """
    计算箱子在指定位置的直接支撑面积

    使用Shapely库计算箱子底面与下方支撑箱子顶面的交集面积。

    Args:
        point: 箱子位置 {'x': float, 'y': float, 'z': float}
        dims: 箱子尺寸 {'length': float, 'width': float, 'height': float}
        placed_boxes: 已放置的箱子列表，每个箱子包含 'position', 'length', 'width', 'height'

    Returns:
        支撑面积（平方毫米）

    Notes:
        - 如果箱子在地面上（z=0），返回箱子底面积
        - 如果没有支撑箱子，返回0.0
        - 使用1e-5的容差判断箱子是否在同一高度

    Examples:
        >>> point = {'x': 0, 'y': 0, 'z': 100}
        >>> dims = {'length': 100, 'width': 100, 'height': 100}
        >>> placed = [{
        ...     'position': {'x': 0, 'y': 0, 'z': 0},
        ...     'length': 100,
        ...     'width': 100,
        ...     'height': 100
        ... }]
        >>> calculate_direct_supported_area(point, dims, placed)
        10000.0
    """
    # 地面上的箱子，全部支撑
    if point['z'] == 0:
        return dims['length'] * dims['width']

    # 创建上层箱子的底面多边形
    upper_footprint = shapely_box(
        point['x'],
        point['y'],
        point['x'] + dims['length'],
        point['y'] + dims['width']
    )

    # 找到所有顶面与当前箱子底面齐平的支撑箱子
    supporters_footprints = [
        shapely_box(
            box['position']['x'],
            box['position']['y'],
            box['position']['x'] + box['length'],
            box['position']['y'] + box['width']
        )
        for box in placed_boxes
        if abs((box['position']['z'] + box['height']) - point['z']) < 1e-5
    ]

    if not supporters_footprints:
        return 0.0

    # 计算交集面积
    return upper_footprint.intersection(unary_union(supporters_footprints)).area


def direct_support_ratio(
    point: Dict[str, float],
    dims: Dict[str, float],
    placed_boxes: List[Dict]
) -> float:
    """
    计算箱子在指定位置的支撑比例

    支撑比例 = 支撑面积 / 箱子底面积

    Args:
        point: 箱子位置 {'x': float, 'y': float, 'z': float}
        dims: 箱子尺寸 {'length': float, 'width': float, 'height': float}
        placed_boxes: 已放置的箱子列表

    Returns:
        支撑比例（0.0 到 1.0）

    Examples:
        >>> point = {'x': 0, 'y': 0, 'z': 100}
        >>> dims = {'length': 100, 'width': 100, 'height': 100}
        >>> placed = [{
        ...     'position': {'x': 0, 'y': 0, 'z': 0},
        ...     'length': 100,
        ...     'width': 100,
        ...     'height': 100
        ... }]
        >>> direct_support_ratio(point, dims, placed)
        1.0
    """
    base_area = dims['length'] * dims['width']
    if base_area <= 0:
        return 0.0
    return calculate_direct_supported_area(point, dims, placed_boxes) / base_area
