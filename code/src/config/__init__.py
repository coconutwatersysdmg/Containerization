"""
配置管理模块

提供装箱系统的所有可配置参数，支持从YAML文件加载或通过API传入。
"""

from .constants import *
from .pallet_config import PalletConfig
from .algorithm_config import (
    PackingAlgorithmConfig,
    RobotSuctionConfig,
    RescueConfig,
    SmallBoxDetectionConfig,
    ExcelDataConfig,
)

__all__ = [
    # 常量
    "PALLET_INDEX_TARGETS",
    "MAX_BOX_GAP_MM",
    "ENABLE_EXPENSIVE_FAILED_REPACK",
    "PROJECT_ROOT",
    "DATA_DIR",
    "OUTPUT_DIR",
    # 配置类
    "PalletConfig",
    "PackingAlgorithmConfig",
    "RobotSuctionConfig",
    "RescueConfig",
    "SmallBoxDetectionConfig",
    "ExcelDataConfig",
]
