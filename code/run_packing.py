"""
新装箱入口

完全使用 src/ 内模块装配 PackingWorkflow，不依赖 zhuangxiang.py。

用法:
    python run_packing.py
"""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

import pandas as pd

from src.config import ENABLE_EXPENSIVE_FAILED_REPACK, OUTPUT_DIR, PALLET_INDEX_TARGETS
from src.data import load_boxes, load_boxes_from_api
from src.geometry import validate_center_of_mass
from src.main import PackingWorkflow, build_json_output_plan
from src.main.report_persister import JsonFileReportPersister
from src.packing import (
    BeamSearchPacker,
    build_centered_single_box_solution,
    build_direct_layer_packing_solution,
)
from src.rescue import (
    FailedPoolRebuilder,
    LowFillRepacker,
    LowLoadRebuilder,
    RescueOptimizer,
    TailFragmentAbsorber,
    fast_rescue_failed_pallets_by_hole_fill,
    fast_rescue_failed_pallets_by_topup,
    rescue_by_recipe_rebuild,
)


class _DynamicRescueOptimizer:
    """为每个分组按其 pallet_dims 懒构造 RescueOptimizer。"""

    def __init__(self, enable_expensive_repack: bool):
        self._enable = enable_expensive_repack
        self._cache: dict = {}

    def optimize_failed_by_failed(self, type_plans, target_mpm):
        pallet_dims = {}
        for plan in type_plans:
            for item in plan.get('packed_items', []):
                pd_info = item.get('pallet_dims')
                if pd_info:
                    pallet_dims = pd_info
                    break
            if pallet_dims:
                break
        key = (
            pallet_dims.get('length', 0),
            pallet_dims.get('width', 0),
            pallet_dims.get('height', 0),
        )
        if key not in self._cache:
            self._cache[key] = RescueOptimizer(
                pallet_dims=pallet_dims,
                enable_expensive_repack=self._enable,
            )
        return self._cache[key].optimize_failed_by_failed(type_plans, target_mpm)


def build_workflow() -> PackingWorkflow:
    """组装 PackingWorkflow。所有原语来自 src/。"""
    return PackingWorkflow(
        preprocess_fn=load_boxes_from_api,
        custom_packer_cls=BeamSearchPacker,
        build_direct_layer_solution=build_direct_layer_packing_solution,
        build_centered_single_box_solution=build_centered_single_box_solution,
        validate_center_of_mass=validate_center_of_mass,
        fast_rescue_hole_fill=fast_rescue_failed_pallets_by_hole_fill,
        fast_rescue_topup=fast_rescue_failed_pallets_by_topup,
        rescue_by_recipe_rebuild=rescue_by_recipe_rebuild,
        rescue_optimizer=_DynamicRescueOptimizer(
            enable_expensive_repack=ENABLE_EXPENSIVE_FAILED_REPACK
        ),
        failed_pool_rebuilder=FailedPoolRebuilder(
            custom_packer_cls=BeamSearchPacker,
            build_direct_layer_solution=build_direct_layer_packing_solution,
            validate_center_of_mass=validate_center_of_mass,
        ),
        low_fill_repacker=LowFillRepacker(
            custom_packer_cls=BeamSearchPacker,
            build_direct_layer_solution=build_direct_layer_packing_solution,
            validate_center_of_mass=validate_center_of_mass,
        ),
        tail_fragment_absorber=TailFragmentAbsorber(),
        low_load_rebuilder=LowLoadRebuilder(
            custom_packer_cls=BeamSearchPacker,
            build_direct_layer_solution=build_direct_layer_packing_solution,
            validate_center_of_mass=validate_center_of_mass,
        ),
        make_json_output_plan=build_json_output_plan,
        pallet_index_targets=PALLET_INDEX_TARGETS,
        report_persister=JsonFileReportPersister(
            OUTPUT_DIR,
            lambda fmt: pd.Timestamp.now().strftime(fmt),
        ),
    )


if __name__ == '__main__':
    workflow = build_workflow()
    report = workflow.run()
    if report is None:
        sys.exit(1)
