"""
装箱服务入口（持续运行模式）

双线程架构：
  - 下载线程：每 200 秒向 WCS 接口请求一次库存数据，保存原始 JSON 到 input/
  - 处理线程：按文件名时间顺序逐个读取 input/ 中的 JSON，执行装箱，结果保存到 output/
  - 处理完的输入文件自动移动到 input/processed/

只要不手动终止（Ctrl+C），程序会一直运行。

用法:
    python run_packing.py
"""

import shutil
import sys
import threading
import time
from pathlib import Path

project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

import pandas as pd

from src.config import ENABLE_EXPENSIVE_FAILED_REPACK, OUTPUT_DIR, PALLET_INDEX_TARGETS
from src.data import fetch_and_save_stock_json, load_boxes_from_local_json
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

# ============================================================================
# 配置
# ============================================================================
DOWNLOAD_INTERVAL = 200        # 下载间隔（秒）
INPUT_DIR = project_root / "input"
PROCESSED_DIR = INPUT_DIR / "processed"
BAD_DIR = INPUT_DIR / "bad"


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
    """组装 PackingWorkflow。所有原语来自 src/。

    注意：preprocess_fn 设为一个占位函数（不会被调用），
    因为持续模式下我们直接使用 workflow.run_with_boxes()。
    """
    return PackingWorkflow(
        preprocess_fn=lambda *a, **k: None,   # 占位，不再使用
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


# ============================================================================
# 下载线程：每 DOWNLOAD_INTERVAL 秒请求一次接口，保存原始 JSON
# ============================================================================
def _download_worker(stop_event: threading.Event):
    """生产者：定时下载库存 JSON 到 input/ 目录。"""
    print(f"[下载线程] 启动，每 {DOWNLOAD_INTERVAL} 秒请求一次接口...")
    while not stop_event.is_set():
        fetch_and_save_stock_json(INPUT_DIR)

        # 分段等待，以便快速响应 stop_event
        for _ in range(DOWNLOAD_INTERVAL):
            if stop_event.is_set():
                break
            time.sleep(1)

    print("[下载线程] 已停止。")


# ============================================================================
# 处理线程：按时间顺序逐个处理 input/ 中的 JSON 文件
# ============================================================================
def _get_pending_files() -> list:
    """获取 input/ 目录下待处理的 .json 文件列表（按文件名排序）。"""
    if not INPUT_DIR.exists():
        return []
    files = sorted(INPUT_DIR.glob("*.json"))
    return files


def _process_worker(stop_event: threading.Event, workflow: PackingWorkflow):
    """消费者：按顺序读取 → 装箱 → 移动到 processed/。"""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    BAD_DIR.mkdir(parents=True, exist_ok=True)

    print("[处理线程] 启动，等待 input/ 目录中出现 JSON 文件...")
    while not stop_event.is_set():
        pending = _get_pending_files()
        if not pending:
            # 没有待处理文件，等 5 秒再看
            for _ in range(5):
                if stop_event.is_set():
                    break
                time.sleep(1)
            continue

        # 取最早的一个文件处理
        filepath = pending[0]
        print(f"\n{'='*60}")
        print(f"[处理] 开始处理: {filepath.name}")
        print(f"{'='*60}")

        try:
            boxes = load_boxes_from_local_json(str(filepath))
            if boxes is None or len(boxes) == 0:
                print(f"[处理] 文件 {filepath.name} 数据为空或异常，移至 bad/")
                shutil.move(str(filepath), str(BAD_DIR / filepath.name))
                continue

            report = workflow.run_with_boxes(boxes)
            if report is None:
                print(f"[处理] 装箱失败: {filepath.name}")

            # 处理完毕，移动到 processed/
            shutil.move(str(filepath), str(PROCESSED_DIR / filepath.name))
            print(f"[处理] 完成: {filepath.name} → processed/")

        except Exception as exc:
            print(f"[处理] 异常: {filepath.name} → {exc}")
            try:
                shutil.move(str(filepath), str(BAD_DIR / filepath.name))
            except Exception:
                pass

    print("[处理线程] 已停止。")


# ============================================================================
# 主入口
# ============================================================================
if __name__ == '__main__':
    print("=" * 60)
    print("装箱服务已启动（持续运行模式）")
    print(f"  下载间隔: {DOWNLOAD_INTERVAL} 秒")
    print(f"  输入目录: {INPUT_DIR}")
    print(f"  输出目录: {OUTPUT_DIR}")
    print("  按 Ctrl+C 停止")
    print("=" * 60)

    # 构建工作流（只构建一次，所有文件共享）
    workflow = build_workflow()

    stop_event = threading.Event()

    downloader = threading.Thread(
        target=_download_worker,
        args=(stop_event,),
        daemon=True,
        name="downloader",
    )
    processor = threading.Thread(
        target=_process_worker,
        args=(stop_event, workflow),
        daemon=True,
        name="processor",
    )

    downloader.start()
    processor.start()

    try:
        # 主线程阻塞，直到用户按 Ctrl+C
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n[主线程] 收到 Ctrl+C，正在停止...")
        stop_event.set()
        downloader.join(timeout=10)
        processor.join(timeout=10)
        print("[主线程] 服务已停止。")
