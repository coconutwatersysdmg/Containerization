"""
DeepPack3D 约束版装箱器（方案 A）

在真实 mm 托盘上复用 DeepPack3D 的空间划分 + 四种启发式选点，
每个候选放置均通过当前项目的业务校验（间隙、支撑、吸盘、叠放等），
并按指数目标封盘。
"""

from __future__ import annotations

import sys
import time
from copy import deepcopy
from itertools import permutations
from typing import Dict, List, Optional, Tuple

from benchmark.deeppack3d_heuristics import HEURISTIC_MAP, indices
from benchmark.deeppack3d_benchmark import REFERENCE_ROOT


def _ensure_reference_on_path() -> None:
    ref = str(REFERENCE_ROOT.resolve())
    if ref not in sys.path:
        sys.path.insert(0, ref)
    if not REFERENCE_ROOT.exists():
        raise FileNotFoundError(f"未找到参考项目目录: {REFERENCE_ROOT}")


def _int_dims(pallet_dims: Dict[str, float]) -> Tuple[int, int, int]:
    """DeepPack 网格尺寸 (w, h, d) ↔ 项目 (length, height, width)。"""
    length = int(round(float(pallet_dims.get("length", 0) or 0)))
    height = int(round(float(pallet_dims.get("height", 0) or 0)))
    width = int(round(float(pallet_dims.get("width", 0) or 0)))
    if min(length, width, height) <= 0:
        raise ValueError(f"无效托盘尺寸: {pallet_dims}")
    return length, height, width


def _rotated_sizes(length: int, height: int, width: int) -> List[Tuple[int, int, int]]:
    """6 种朝向 (w, h, d) = (length轴, 竖直, width轴)。"""
    base = (length, height, width)
    sizes = {base}
    w, h, d = base
    sizes.add((w, d, h))
    sizes.add((d, h, w))
    sizes.add((h, w, d))
    sizes.add((d, w, h))
    sizes.add((h, d, w))
    return list(sizes)


def _rotated_orientations(
    raw: Dict[str, float],
    xy_tol: float,
    z_tol: float,
) -> List[Tuple[int, int, int, Dict[str, float]]]:
    """返回 (w, h, d) 及与托盘轴对齐的 raw 尺寸（用于间隙/吸盘校验）。"""
    eff = {
        "length": float(raw["length"]) + xy_tol,
        "height": float(raw["height"]) + z_tol,
        "width": float(raw["width"]) + xy_tol,
    }
    axis_names = ("length", "height", "width")
    seen: set = set()
    orientations: List[Tuple[int, int, int, Dict[str, float]]] = []

    for perm in permutations((0, 1, 2)):
        w_axis = axis_names[perm[0]]
        h_axis = axis_names[perm[1]]
        d_axis = axis_names[perm[2]]
        w = int(round(eff[w_axis]))
        h = int(round(eff[h_axis]))
        d = int(round(eff[d_axis]))
        key = (w, h, d)
        if key in seen:
            continue
        seen.add(key)
        oriented_raw = {
            "length": float(raw[w_axis]),
            "height": float(raw[h_axis]),
            "width": float(raw[d_axis]),
        }
        orientations.append((w, h, d, oriented_raw))

    return orientations


def _item_effective_dims(item: Dict, xy_tol: float, z_tol: float) -> Dict[str, float]:
    raw_length = float(item.get("raw_length", item.get("length", 0)) or 0)
    raw_width = float(item.get("raw_width", item.get("width", 0)) or 0)
    raw_height = float(item.get("raw_height", item.get("height", 0)) or 0)
    return {
        "length": raw_length + xy_tol,
        "width": raw_width + xy_tol,
        "height": raw_height + z_tol,
    }


def _to_project_placement(
    item: Dict,
    cuboid_x: int,
    cuboid_y: int,
    cuboid_z: int,
    w: int,
    h: int,
    d: int,
    xy_tol: float,
    z_tol: float,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """DeepPack Cuboid 坐标 → 当前项目 position + dims。"""
    point = {"x": float(cuboid_x), "y": float(cuboid_z), "z": float(cuboid_y)}
    raw_length = float(item.get("raw_length", item.get("length", 0)) or 0)
    raw_width = float(item.get("raw_width", item.get("width", 0)) or 0)
    raw_height = float(item.get("raw_height", item.get("height", 0)) or 0)
    dims = {
        "length": float(w),
        "width": float(d),
        "height": float(h),
    }
    if abs(dims["length"] - (raw_length + xy_tol)) > 1e-6:
        pass  # rotation applied; keep oriented dims
    return point, dims


class DeepPack3DConstrainedPacker:
    """带完整业务约束的 DeepPack3D 启发式装箱器。"""

    def __init__(
        self,
        method: str = "bl",
        lookahead: int = 5,
        support_ratio_threshold: float = 0.8,
        size_tolerance: float = 2.0,
        z_tolerance: float = 0.0,
        stop_when_target_met: bool = True,
    ):
        if method not in HEURISTIC_MAP:
            raise ValueError(f"不支持的方法: {method}")
        self.method = method
        self.heuristic = HEURISTIC_MAP[method]
        self.lookahead = max(1, int(lookahead))
        self.support_ratio_threshold = support_ratio_threshold
        self.size_tolerance = size_tolerance
        self.z_tolerance = z_tolerance
        self.stop_when_target_met = stop_when_target_met

    def pack_group(
        self,
        pallet_type: str,
        sales_order_no: str,
        boxes_in_group: List[Dict],
        target_mpm: Optional[float],
    ) -> Tuple[List[Dict], Dict[str, float], Dict]:
        from src.geometry.center_of_mass import validate_center_of_mass
        from src.geometry.constraint_validator import validate_pallet_constraints
        from src.packing.placement_validator import PlacementValidator
        from src.packing.suction_planner import SuctionPlanner
        from src.utils.dimensions import raw_dims

        _ensure_reference_on_path()
        from geometry import Cuboid  # noqa: WPS433
        from SpacePartitioner import SpacePartitioner  # noqa: WPS433

        self._Cuboid = Cuboid
        self._SpacePartitioner = SpacePartitioner

        start = time.time()
        pallet_dims = boxes_in_group[0]["pallet_dims"]
        validator = PlacementValidator(
            pallet_dims=pallet_dims,
            support_ratio_threshold=self.support_ratio_threshold,
            size_tolerance=self.size_tolerance,
            z_tolerance=self.z_tolerance,
        )
        suction = SuctionPlanner(pallet_dims=pallet_dims)

        unfitted = list(boxes_in_group)
        type_plan: List[Dict] = []
        pallet_counter = 1

        while unfitted:
            packed, unfitted = self._pack_one_pallet(
                unfitted,
                pallet_dims,
                target_mpm,
                validator,
                suction,
                raw_dims,
            )
            if not packed:
                break

            total_mpm = sum(float(b.get("min_pack_multiple", 0) or 0) for b in packed)
            mpm_gap = None if target_mpm is None else (target_mpm - total_mpm)
            if target_mpm is None:
                mpm_status = "UNKNOWN"
            elif total_mpm >= target_mpm:
                mpm_status = "SUCCESS"
            else:
                mpm_status = "FAILED"

            solution = {
                "pallet_id": f"{pallet_type}-{sales_order_no}-{pallet_counter}",
                "pallet_type": pallet_type,
                "sales_order_no": sales_order_no,
                "packed_items": packed,
                "mpm_total": total_mpm,
                "mpm_target": target_mpm,
                "mpm_gap": mpm_gap,
                "mpm_status": mpm_status,
                "packer_backend": f"deeppack3d_constrained_{self.method}",
            }

            gate = validate_pallet_constraints(solution, pallet_dims)
            hard_violations = [
                v for v in gate["violations"] if v.get("type") != "center_of_mass"
            ]
            if hard_violations:
                raise RuntimeError(
                    "DeepPack 约束版输出违反业务规则: "
                    f"{hard_violations[:3]}"
                )

            com = validate_center_of_mass(solution, pallet_dims)
            solution["stability_checks"] = {
                "status": "SUCCESS" if com.get("is_stable") else "FAILED",
            }
            if not com.get("is_stable"):
                solution["stability_checks"]["center_of_mass_failure"] = com

            type_plan.append(solution)
            pallet_counter += 1

        runtime = {"packing": round(time.time() - start, 4), "retry": 0.0}
        diag = {"method": self.method, "lookahead": self.lookahead}
        return type_plan, runtime, diag

    def _pack_one_pallet(
        self,
        unfitted: List[Dict],
        pallet_dims: Dict,
        target_mpm: Optional[float],
        validator,
        suction,
        raw_dims_fn,
    ) -> Tuple[List[Dict], List[Dict]]:
        from src.geometry.gap_checker import passes_box_gap_constraint

        w_bin, h_bin, d_bin = _int_dims(pallet_dims)
        partitioner = self._SpacePartitioner((w_bin, h_bin, d_bin))
        placed: List[Dict] = []
        pool = list(unfitted)

        while pool:
            current_mpm = sum(float(b.get("min_pack_multiple", 0) or 0) for b in placed)
            target_met = (
                target_mpm is not None
                and current_mpm >= float(target_mpm) - 1e-9
            )
            if self.stop_when_target_met and target_met:
                from src.geometry.center_of_mass import validate_center_of_mass

                com = validate_center_of_mass(
                    {"packed_items": placed}, pallet_dims
                )
                if com.get("is_stable"):
                    break

            window = pool[: self.lookahead]
            actions = self._build_valid_actions(
                window,
                partitioner,
                placed,
                validator,
                suction,
                raw_dims_fn,
                passes_box_gap_constraint,
            )
            if not actions or not indices(actions):
                break

            item_idx, _bin_idx, placement_idx = self._select_action(
                actions, placed, pallet_dims
            )
            placement = actions[item_idx][0][placement_idx]
            _, (_x, _y, _z), (_w, _h, _d), _split = placement
            chosen = window[item_idx]
            placed_item = actions[item_idx][0][placement_idx][0]
            if placed_item is None:
                break

            cuboid = self._Cuboid(int(_x), int(_y), int(_z), int(_w), int(_h), int(_d))
            if not partitioner.add(cuboid):
                break

            placed.append(placed_item)
            pool.remove(chosen)
            unfitted.remove(chosen)

        from src.packing.sanitizer import sanitize_packed_items

        placed, removed = sanitize_packed_items(
            placed,
            support_ratio_threshold=self.support_ratio_threshold,
            max_gap=6.0,
            pallet_dims=pallet_dims,
        )
        if removed:
            unfitted = removed + unfitted

        return placed, unfitted

    def _build_valid_actions(
        self,
        window: List[Dict],
        partitioner,
        placed: List[Dict],
        validator,
        suction,
        raw_dims_fn,
        passes_box_gap_constraint,
    ) -> List[List[List[tuple]]]:
        """构造 DeepPack 启发式所需的 actions 结构（仅含通过业务校验的候选）。"""
        actions: List[List[List[tuple]]] = []
        h_map = partitioner.height_map

        for item in window:
            item_actions: List[tuple] = []
            raw = raw_dims_fn(item)
            item_volume = raw["length"] * raw["width"] * raw["height"]

            orientations = _rotated_orientations(
                raw, self.size_tolerance, self.z_tolerance
            )

            for w, h, d, oriented_raw in orientations:
                if w > partitioner.size[0] or h > partitioner.size[1] or d > partitioner.size[2]:
                    continue

                for split in partitioner.free_splits:
                    if not split.fit((w, h, d)):
                        continue

                    candidates = self._placement_coords(
                        partitioner, split, h_map, w, h, d
                    )
                    for x, y, z in candidates:
                        point, dims = _to_project_placement(
                            item, x, y, z, w, h, d,
                            self.size_tolerance, self.z_tolerance,
                        )
                        if not validator.is_within_bounds(point, dims):
                            continue
                        if validator.check_overlap(point, dims, placed):
                            continue
                        if not validator.satisfies_size_order(
                            point, dims, item_volume, placed
                        ):
                            continue
                        if not validator.satisfies_stacking_order(
                            item, point, dims, placed
                        ):
                            continue
                        if not validator.is_stable(point, dims, placed):
                            continue
                        if not passes_box_gap_constraint(
                            point, dims, oriented_raw, placed, max_gap=6.0
                        ):
                            continue

                        suction_pose = suction.find_reachable_suction_pose(
                            point, dims, placed, raw_dims=oriented_raw
                        )
                        if suction_pose is None:
                            continue

                        trial = self._make_placed_item(
                            item, point, dims, oriented_raw, suction_pose
                        )

                        item_actions.append(
                            (
                                trial,
                                (x, y, z),
                                (w, h, d),
                                split,
                            )
                        )

            actions.append([item_actions])

        return actions

    def _select_action(
        self,
        actions: List[List[List[tuple]]],
        placed: List[Dict],
        pallet_dims: Dict,
    ) -> Tuple[int, int, int]:
        """在启发式排序基础上优先选择更利于重心稳定的放置。"""
        from src.geometry.center_of_mass import validate_center_of_mass

        ranked: List[Tuple[tuple, Tuple[int, int, int]]] = []
        for i, item in enumerate(actions):
            for j, bin_ in enumerate(item):
                for k, placement in enumerate(bin_):
                    trial = placement[0]
                    com = validate_center_of_mass(
                        {"packed_items": placed + [trial]}, pallet_dims
                    )
                    if com.get("is_stable"):
                        com_key = (0.0, 0.0, 0.0)
                    else:
                        com_key = (
                            1.0,
                            abs(float(com.get("offset_x", 0.0) or 0.0)),
                            abs(float(com.get("offset_y", 0.0) or 0.0)),
                        )
                    ranked.append(
                        (com_key + self._heuristic_rank_key(placement), (i, j, k))
                    )

        ranked.sort(key=lambda row: row[0])
        return ranked[0][1]

    @staticmethod
    def _heuristic_rank_key(placement: tuple) -> tuple:
        _, (x, y, z), (w, h, d), split = placement
        return (y + h, x + w, z + d, split.volume, min(split.width - w, split.height - h))

    @staticmethod
    def _placement_coords(partitioner, split, h_map, w, h, d) -> List[Tuple[int, int, int]]:
        """参考 env.placeable_coords：split 角点 + 高度图支撑 + 空盘居中候选。"""
        coords: List[Tuple[int, int, int]] = []
        x0, y0, z0 = split.coord
        coords.append((x0, y0, z0))

        bin_w, _bin_h, bin_d = partitioner.size
        if int(h_map.max()) == 0:
            center_x = max(0, (bin_w - w) // 2)
            center_z = max(0, (bin_d - d) // 2)
            coords.append((center_x, 0, center_z))

        xz_seen = set()
        for sp in partitioner.free_splits:
            if sp.top < partitioner.size[1] and sp.fit((w, h, d)):
                x, y, z = sp.coord
                key = (x, z)
                if key in xz_seen:
                    continue
                xz_seen.add(key)
                region = h_map[z : z + d, x : x + w]
                if region.size == 0:
                    continue
                y_top = int(region.max())
                supported = int((region == y_top).sum())
                if supported / max(1, d * w) <= 0.5:
                    continue
                coords.append((x, y_top, z))

        uniq = []
        seen = set()
        for c in coords:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq

    @staticmethod
    def _make_placed_item(
        item: Dict,
        point: Dict[str, float],
        dims: Dict[str, float],
        raw: Dict[str, float],
        suction_pose: Dict,
    ) -> Dict:
        placed_item = deepcopy(item)
        placed_item["position"] = dict(point)
        placed_item["raw_length"] = raw["length"]
        placed_item["raw_width"] = raw["width"]
        placed_item["raw_height"] = raw["height"]
        placed_item["length"] = dims["length"]
        placed_item["width"] = dims["width"]
        placed_item["height"] = dims["height"]
        placed_item["suction_box_corner"] = suction_pose["box_corner"]
        placed_item["suction_cup_corner"] = suction_pose["cup_corner"]
        placed_item["suction_orientation"] = suction_pose["orientation"]
        placed_item["suction_cup_x_size"] = suction_pose["cup_x_size"]
        placed_item["suction_cup_y_size"] = suction_pose["cup_y_size"]
        placed_item["suction_rect_x_min"] = suction_pose["cup_rect"]["x_min"]
        placed_item["suction_rect_x_max"] = suction_pose["cup_rect"]["x_max"]
        placed_item["suction_rect_y_min"] = suction_pose["cup_rect"]["y_min"]
        placed_item["suction_rect_y_max"] = suction_pose["cup_rect"]["y_max"]
        return placed_item
