"""Full pallet constraint validation.

This module is used as a stage gate after every packing or rescue mutation.
It does not repair a pallet.  It only reports whether the current placement
still satisfies the hard geometric and handling constraints.
"""

from typing import Dict, List

from .center_of_mass import validate_center_of_mass
from .gap_checker import passes_box_gap_constraint
from .overlap import axis_overlap_len
from .support import direct_support_ratio


REQUIRED_SUCTION_FIELDS = (
    "suction_box_corner",
    "suction_cup_corner",
    "suction_orientation",
    "suction_cup_x_size",
    "suction_cup_y_size",
    "suction_rect_x_min",
    "suction_rect_x_max",
    "suction_rect_y_min",
    "suction_rect_y_max",
)


def validate_pallet_constraints(
    pallet_plan: Dict,
    pallet_dims: Dict[str, float],
    support_ratio_threshold: float = 0.8,
    max_gap: float = 6.0,
    require_suction: bool = True,
) -> Dict:
    """Validate all hard constraints for one pallet plan."""
    violations: List[Dict] = []
    items = pallet_plan.get("packed_items", []) or []
    pallet_length = float(pallet_dims.get("length", 0) or 0)
    pallet_width = float(pallet_dims.get("width", 0) or 0)
    pallet_height = float(pallet_dims.get("height", 0) or 0)

    for idx, item in enumerate(items):
        item_id = item.get("id")
        pos = item.get("position")
        if not pos:
            violations.append({
                "type": "missing_position",
                "box_id": item_id,
            })
            continue

        dims = _dims(item)
        if (
            pos.get("x", 0) < -1e-9
            or pos.get("y", 0) < -1e-9
            or pos.get("z", 0) < -1e-9
            or pos.get("x", 0) + dims["length"] > pallet_length + 1e-9
            or pos.get("y", 0) + dims["width"] > pallet_width + 1e-9
            or pos.get("z", 0) + dims["height"] > pallet_height + 1e-9
        ):
            violations.append({
                "type": "out_of_bounds",
                "box_id": item_id,
            })

        if require_suction:
            missing = [
                field for field in REQUIRED_SUCTION_FIELDS
                if item.get(field) is None
            ]
            if missing:
                violations.append({
                    "type": "missing_suction",
                    "box_id": item_id,
                    "fields": missing,
                })

        others = [
            other for other in items
            if other.get("id") != item_id and other.get("position")
        ]
        raw = {
            "length": float(item.get("raw_length", item.get("length", 0)) or 0),
            "width": float(item.get("raw_width", item.get("width", 0)) or 0),
            "height": float(item.get("raw_height", item.get("height", 0)) or 0),
        }
        if not passes_box_gap_constraint(
            pos, dims, raw, others, max_gap=max_gap
        ):
            violations.append({
                "type": "gap",
                "box_id": item_id,
            })

        if pos.get("z", 0) > 1e-9:
            ratio = direct_support_ratio(pos, dims, others)
            if ratio + 1e-9 < support_ratio_threshold:
                violations.append({
                    "type": "support",
                    "box_id": item_id,
                    "support_ratio": ratio,
                })

        for other in items[idx + 1:]:
            other_pos = other.get("position")
            if not other_pos:
                continue
            other_dims = _dims(other)
            if (
                axis_overlap_len(
                    pos["x"], pos["x"] + dims["length"],
                    other_pos["x"], other_pos["x"] + other_dims["length"],
                ) > 1e-9
                and axis_overlap_len(
                    pos["y"], pos["y"] + dims["width"],
                    other_pos["y"], other_pos["y"] + other_dims["width"],
                ) > 1e-9
                and axis_overlap_len(
                    pos["z"], pos["z"] + dims["height"],
                    other_pos["z"], other_pos["z"] + other_dims["height"],
                ) > 1e-9
            ):
                violations.append({
                    "type": "overlap",
                    "box_id": item_id,
                    "other_box_id": other.get("id"),
                })

    if items and not any(v["type"] == "missing_position" for v in violations):
        com = validate_center_of_mass(pallet_plan, pallet_dims)
        if not com.get("is_stable", False):
            violations.append({
                "type": "center_of_mass",
                "detail": com,
            })

    return {
        "is_valid": not violations,
        "violations": violations,
    }


def validate_plan_constraints(
    plans: List[Dict],
    pallet_dims: Dict[str, float],
    **kwargs,
) -> Dict:
    """Validate all non-empty pallets in a group."""
    invalid = []
    for plan in plans:
        if not plan.get("packed_items"):
            continue
        result = validate_pallet_constraints(plan, pallet_dims, **kwargs)
        if not result["is_valid"]:
            invalid.append({
                "pallet_id": plan.get("pallet_id"),
                "violations": result["violations"],
            })
    return {
        "is_valid": not invalid,
        "invalid_pallets": invalid,
    }


def _dims(item: Dict) -> Dict[str, float]:
    return {
        "length": float(item.get("length", 0) or 0),
        "width": float(item.get("width", 0) or 0),
        "height": float(item.get("height", 0) or 0),
    }
