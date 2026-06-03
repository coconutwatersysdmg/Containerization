"""
装箱工作流编排器

整合 OrderProcessor、PalletPacker、救援链与 ResultFormatter，
驱动端到端装箱流程。
"""

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .order_processor import OrderProcessor
from .pallet_packer import PalletPacker
from .result_formatter import ResultFormatter


class PackingWorkflow:
    """端到端装箱工作流。"""

    def __init__(
        self,
        preprocess_fn: Callable,
        custom_packer_cls,
        build_direct_layer_solution: Callable,
        build_centered_single_box_solution: Callable,
        validate_center_of_mass: Callable,
        fast_rescue_hole_fill: Callable,
        fast_rescue_topup: Callable,
        rescue_by_recipe_rebuild: Callable,
        rescue_optimizer,
        failed_pool_rebuilder,
        low_fill_repacker,
        tail_fragment_absorber,
        low_load_rebuilder,
        make_json_output_plan: Callable,
        pallet_index_targets: Dict[str, float],
        report_persister=None,
    ):
        self.order_processor = OrderProcessor(preprocess_fn)
        self.packer = PalletPacker(
            custom_packer_cls,
            build_direct_layer_solution,
            build_centered_single_box_solution,
            validate_center_of_mass,
        )
        self._hole_fill = fast_rescue_hole_fill
        self._topup = fast_rescue_topup
        self._recipe_rebuild = rescue_by_recipe_rebuild
        self._rescue_optimizer = rescue_optimizer
        self._failed_pool_rebuilder = failed_pool_rebuilder
        self._low_fill_repacker = low_fill_repacker
        self._tail_fragment_absorber = tail_fragment_absorber
        self._low_load_rebuilder = low_load_rebuilder
        self._make_json_plan = make_json_output_plan
        self._targets = pallet_index_targets
        self._report_persister = report_persister

    def run(self, data_filepath: Optional[str] = None) -> Optional[Dict]:
        start = time.time()
        all_boxes, grouped = self.order_processor.prepare(data_filepath)
        return self._run_with_prepared(all_boxes, grouped, start)

    def run_with_boxes(self, boxes: List[Dict]) -> Optional[Dict]:
        start = time.time()
        grouped = self.order_processor.group_by_order(boxes or [])
        return self._run_with_prepared(boxes or [], grouped, start)

    def _run_with_prepared(
        self,
        all_boxes: List[Dict],
        grouped: Dict,
        start: float,
    ) -> Optional[Dict]:
        if not all_boxes:
            return None
        print("数据预处理和箱子分组完成（按托盘类型+销售订单号）。\n" + "-" * 40)

        final_plan: List[Dict] = []
        runtime_stats = {
            "group_pack_seconds": 0.0,
            "group_topup_seconds": 0.0,
            "group_retry_seconds": 0.0,
            "group_repack_seconds": 0.0,
            "group_total_seconds": 0.0,
        }
        by_type_stats: Dict[str, Dict] = {}

        for (pallet_type, sales_order_no), boxes_in_group in grouped.items():
            group_start = time.time()
            print(f"正在处理托盘类型：{pallet_type}，销售订单号：{sales_order_no}")
            target_mpm = self._targets.get(pallet_type)
            if target_mpm is None:
                print(f"  - 警告：托盘类型 {pallet_type} 未配置指数目标，将退化为 mpm 总量优先。")

            type_plan, pack_runtime, index_diag = self.packer.pack_group(
                pallet_type, sales_order_no, boxes_in_group, target_mpm
            )
            self._print_diagnostics(index_diag, target_mpm)

            pallet_dims = boxes_in_group[0]['pallet_dims']
            repack_start = time.time()
            canonical = (index_diag.get("canonical_layer_best") or {}).get("best_mpm")
            geometric_unreachable = (
                target_mpm is not None
                and canonical is not None
                and float(canonical) + 1e-9 < float(target_mpm)
            )
            if geometric_unreachable:
                print(f"  - 几何不可达标记：典型整层上限 {float(canonical):g} < 目标 {float(target_mpm):g}，救援优先压缩托盘数和填充率。")

            main_tail_diag = index_diag.get("main_tail_absorb") or {}
            skip_index_rescue = geometric_unreachable or main_tail_diag.get("tail_absorb_success", 0) > 0
            rescue_timing = {}

            t_stage = time.time()
            pool_diag = {"rescued": 0, "rebuild_attempts": 0, "skipped": True} if geometric_unreachable else self._failed_pool_rebuilder.rebuild(type_plan, pallet_dims, target_mpm)
            rescue_timing["failed_pool_seconds"] = time.time() - t_stage

            t_stage = time.time()
            hf = {"rescued": 0, "hole_fill_tried": 0, "hole_fill_pack_fail": 0, "skipped": True} if skip_index_rescue else self._hole_fill(type_plan, pallet_dims, target_mpm, max_gap=64.0, max_attempts=80, max_donor_scan=160, max_add_items=8)
            rescue_timing["hole_fill_seconds"] = time.time() - t_stage

            t_stage = time.time()
            tu = {"rescued": 0, "topup_tried": 0, "topup_pack_fail": 0, "topup_rejected_missing_receiver": 0, "skipped": True} if skip_index_rescue else self._topup(type_plan, pallet_dims, target_mpm, max_gap=64.0, max_attempts=80, max_donor_scan=80)
            rescue_timing["topup_rescue_seconds"] = time.time() - t_stage

            t_stage = time.time()
            rb = {"rescued": 0, "recipe_rebuild_tried": 0, "recipe_rebuild_success": 0, "skipped": True} if geometric_unreachable else self._recipe_rebuild(type_plan, pallet_dims, target_mpm, max_group_boxes=400, max_recipe_count=12)
            rescue_timing["recipe_rebuild_seconds"] = time.time() - t_stage

            t_stage = time.time()
            repack = self._rescue_optimizer.optimize_failed_by_failed(type_plan, target_mpm)
            rescue_timing["pair_repack_seconds"] = time.time() - t_stage

            t_stage = time.time()
            low_fill_diag = {"low_fill_tried": 0, "low_fill_accepted": 0, "reason": "geometric_target_unreachable"} if geometric_unreachable else self._low_fill_repacker.repack(type_plan, pallet_dims, target_mpm, geometric_unreachable)
            rescue_timing["low_fill_seconds"] = time.time() - t_stage

            t_stage = time.time()
            tail_diag = {"tail_absorb_tried": 0, "tail_absorb_success": 0, "skipped": True} if main_tail_diag.get("tail_absorb_success", 0) else self._tail_fragment_absorber.absorb(type_plan, pallet_dims, target_mpm)
            rescue_timing["tail_absorb_seconds"] = time.time() - t_stage

            t_stage = time.time()
            low_diag = self._low_load_rebuilder.compact_low_fill_tails(type_plan, pallet_dims, target_mpm) if geometric_unreachable else self._low_load_rebuilder.rebuild(type_plan, pallet_dims, target_mpm)
            rescue_timing["low_load_seconds"] = time.time() - t_stage
            if hasattr(self._low_load_rebuilder, "merge_low_load_pairs"):
                t_stage = time.time()
                low_pair_diag = self._low_load_rebuilder.merge_low_load_pairs(type_plan, pallet_dims, target_mpm)
                rescue_timing["low_pair_seconds"] = time.time() - t_stage
                if low_pair_diag:
                    low_diag.update(low_pair_diag)

            self._drop_empty_pallets(type_plan)
            repack_time = time.time() - repack_start
            rescued = hf.get("rescued", 0) + tu.get("rescued", 0) + rb.get("rescued", 0) + repack.get("rescued", 0) + max(0, low_fill_diag.get("low_fill_new_success", 0) - low_fill_diag.get("low_fill_old_success", 0)) + pool_diag.get("rescued", 0) + max(0, tail_diag.get("tail_absorb_new_success", 0) - tail_diag.get("tail_absorb_old_success", 0)) + max(0, low_diag.get("low_load_new_success", 0) - low_diag.get("low_load_old_success", 0))

            group_total = time.time() - group_start
            runtime = {"packing": pack_runtime["packing"], "topup": pack_runtime.get("topup", 0.0), "retry": pack_runtime["retry"], "repack": repack_time, "total": group_total, **rescue_timing}
            type_stats = ResultFormatter.build_type_stats(type_plan, pallet_type, sales_order_no, index_diag, rescued, runtime, repack, hf, tu, rb, low_diag, tail_diag, low_fill_diag, pool_diag)
            by_type_stats[f"{pallet_type}__{sales_order_no}"] = type_stats
            final_plan.extend(type_plan)
            runtime_stats["group_pack_seconds"] += pack_runtime["packing"]
            runtime_stats["group_topup_seconds"] += pack_runtime.get("topup", 0.0)
            runtime_stats["group_retry_seconds"] += pack_runtime["retry"]
            runtime_stats["group_repack_seconds"] += repack_time
            runtime_stats["group_total_seconds"] += group_total
            self._print_group_summary(type_stats, rescued, pack_runtime["packing"], pack_runtime["retry"], repack_time, group_total, pallet_type, sales_order_no, repack)

        total_runtime = time.time() - start
        summary = {"overall": ResultFormatter.build_overall_summary(final_plan, by_type_stats, runtime_stats, total_runtime), "by_pallet_type": by_type_stats}
        self._print_overall(summary["overall"], runtime_stats, total_runtime)
        report = ResultFormatter.build_full_report(final_plan, summary, total_runtime, all_boxes, self._make_json_plan)
        if self._report_persister is not None:
            self._report_persister.persist(report, total_runtime)
        return report

    def _drop_empty_pallets(self, type_plan: List[Dict]) -> int:
        before = len(type_plan)
        type_plan[:] = [plan for plan in type_plan if plan.get('packed_items')]
        return before - len(type_plan)

    def _print_diagnostics(self, diag: Dict, target_mpm: Optional[float]) -> None:
        if target_mpm is None:
            return
        canonical = (diag.get("canonical_layer_best") or {}).get("best_mpm")
        print(f"  - 指数诊断：箱子 {diag['box_count']} 个，总指数 {diag['total_mpm']:g}，目标 {target_mpm:g}，理论最多达标托盘 {diag['theoretical_success_pallets']} 个，剩余指数 {diag['residual_mpm']:g}。")
        if canonical is not None:
            tail = ' 当前目标可能受托盘高度/箱型组合限制。' if canonical < target_mpm else ''
            print(f"  - 几何诊断：典型整层堆叠单盘参考上限 {canonical:g}/{target_mpm:g}。{tail}")

    def _print_group_summary(self, type_stats, rescued, pack_t, retry_t, repack_t, total_t, pallet_type, sales_order_no, repack) -> None:
        kpi = type_stats["kpi"]
        runtime = type_stats.get("runtime_breakdown_seconds", {})
        print(f"托盘类型 {pallet_type}（销售订单号：{sales_order_no}）处理完成：总托盘 {type_stats['total_pallets']}，指数达标 {type_stats['success_pallets']}，未达标 {type_stats['failed_pallets']}，平均缺口 {type_stats['avg_mpm_gap']:.2f}，最大缺口 {type_stats['max_mpm_gap']:.2f}，失败托盘互借修复成功 {rescued}。")
        print(f"  - KPI：near/mid/deep={kpi['failed_near_count']}/{kpi['failed_mid_count']}/{kpi['failed_deep_count']}，救回率={kpi['rescue_rate_from_failed']:.2%}，pair效率={kpi['pair_efficiency']:.2%}。")
        print(f"  - 耗时拆解：packing={pack_t:.2f}s，main_topup={runtime.get('topup', 0.0):.2f}s，retry={retry_t:.2f}s，failed_pool={runtime.get('failed_pool', 0.0):.2f}s，repack={repack_t:.2f}s，total={total_t:.2f}s。\n")

    def _print_overall(self, overall, runtime_stats, total_runtime):
        kpi = overall["kpi"]
        print("=" * 40)
        print(f"统计汇总：总托盘 {overall['total_pallets']}，指数达标 {overall['success_pallets']}，未达标 {overall['failed_pallets']}，未知 {overall['unknown_pallets']}，平均缺口 {overall['avg_mpm_gap']:.2f}，最大缺口 {overall['max_mpm_gap']:.2f}，失败托盘互借修复成功 {overall['rescued_from_failed']}")
        print(f"KPI汇总：near/mid/deep={kpi['failed_near_count']}/{kpi['failed_mid_count']}/{kpi['failed_deep_count']}，pair尝试={kpi['pair_tried']}，pair改进={kpi['pair_improved']}，pair效率={kpi['pair_efficiency']:.2%}")
        print(f"耗时汇总：packing={runtime_stats['group_pack_seconds']:.2f}s，main_topup={runtime_stats.get('group_topup_seconds', 0.0):.2f}s，retry={runtime_stats['group_retry_seconds']:.2f}s，repack={runtime_stats['group_repack_seconds']:.2f}s，end_to_end={total_runtime:.2f}s")
        print("=" * 40)
