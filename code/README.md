# 装箱算法（code）

`code/` 是当前在用的真实装箱项目。它的目标是：在满足业务规则、几何约束和机械约束的前提下，尽量提高达到目标指数的托盘数量，并保证箱子守恒。

## 核心目标

- 按 `(pallet_type, sales_order_no)` 分组装箱。
- 每个托盘尽量达到对应托盘类型的目标指数。
- 在箱子充足且仍可合法放入时，优先继续装满当前托盘，而不是提前结束。
- 所有箱子最终都要进入托盘；低装载托盘只应在尾盘或确实无可行补箱时出现。

## 业务规则

| 规则 | 说明 | 位置 |
|---|---|---|
| 分组 | 按 `(pallet_type, sales_order_no)` 分组，组内独立装箱 | `src/main/order_processor.py` |
| 指数目标 | `MH423C -> 192`，`MH110 -> 32` | `src/config/constants.py` |
| 达标判定 | 根据 `mpm_total` 与托盘目标指数判定 `SUCCESS / FAILED` | `src/rescue/pallet_evaluator.py` |
| 数据输入 | 支持 Excel 适配器，也支持直接传入标准化 `boxes` | `src/data/excel_loader.py`、`src/main/workflow.py` |
| 箱子守恒 | 允许兜底装箱，但不能跳过箱子 | `src/main/pallet_packer.py` |
| 当前托盘装满优先 | 托盘初始达标后，主流程会继续尝试吸收 `remaining` 中可合法放入的箱子 | `src/main/pallet_packer.py` |

## 装箱约束

### 硬约束

| 约束 | 说明 | 位置 |
|---|---|---|
| 不超界 | 箱子不得超出托盘 `length / width / height` | `src/packing/placement_validator.py` |
| 不重叠 | 箱子之间不得发生几何重叠 | `src/geometry/overlap.py`、`src/packing/placement_validator.py` |
| 箱间间隙 | 相邻箱子在 XY 方向的正向间隙必须 `< 6 mm` | `src/geometry/gap_checker.py` |
| 支撑率 | 非底层箱子的直接支撑率必须 `>= 0.8` | `src/geometry/support.py` |
| 重心稳定 | 整体重心投影必须落在允许区域 | `src/geometry/center_of_mass.py` |
| 吸盘可达 | 吸盘垂直下放路径不得被遮挡 | `src/packing/suction_planner.py` |
| 小箱在下 | 不允许把更小的小箱压在大箱之上 | `src/utils/helpers.py` |
| 同尺寸重箱在下 | 同尺寸箱子发生上下叠放时，重箱必须在下、轻箱必须在上 | `src/packing/stacking_policy.py` |

### 排序/配方偏好

| 规则 | 说明 | 位置 |
|---|---|---|
| 同底面不同高度优先按倍数凑层 | 对同 footprint、不同高度且存在整数倍关系的箱型，优先保留更利于倍数组层的候选 | `src/packing/stacking_policy.py`、`src/packing/layer_pool_builder.py`、`src/packing/beam_search_packer.py` |
| 同尺寸箱排序 | 同尺寸候选优先按重量降序尝试 | `src/packing/stacking_policy.py` |

说明：第二类属于主流程中的排序偏好，不单独替代几何可行性校验。

## 输入与输出

### 输入

- Excel 文件：通过 `src/data/excel_loader.py` 转成标准化 `boxes`
- 或直接调用 `PackingWorkflow.run_with_boxes(boxes)`

标准化箱子至少需要包含以下信息：

- `id`
- `type`
- `length`
- `width`
- `height`
- `weight`
- `pallet_type`
- `sales_order_no`
- `min_pack_multiple`

### 输出

默认本地持久化器会同时输出两份文件：

1. JSON 方案文件  
   `output/packing_plan_<timestamp>.json`

2. 托盘统计 Excel  
   `output/packing_plan_summary_<timestamp>.xlsx`

Excel 包含以下字段：

- `托盘ID`
- `托盘尺寸(mm)`
- `箱子数量`
- `稳定性状态`
- `指数`
- `目标指数`
- `指数缺口`
- `指数状态`

如在接口服务中使用，可注入 `NullReportPersister`，由外层自行返回或保存结果。

## 主流程

主流程由 `PackingWorkflow` 编排，单组装箱由 `PalletPacker` 负责：

1. 加载并标准化箱子数据
2. 按 `(pallet_type, sales_order_no)` 分组
3. 对每个分组循环装箱
4. 单托盘优先尝试：
   - `build_direct_layer_packing_solution()`：整层确定性装箱
   - `BeamSearchPacker.pack()`：通用 beam search
   - `build_centered_single_box_solution()`：少量剩余箱的单箱居中兜底
5. 托盘已达标后，执行主流程补箱：
   - `_top_up_current_pallet_to_fill()`
   - 继续吸收 `remaining` 中仍可合法放入的箱子
   - 只有不存在可行补箱时才结束当前托盘
6. 对失败托盘执行低成本修复：
   - `hole_fill_rescuer.py`
   - `topup_rescuer.py`
   - `recipe_rebuilder.py`
   - `rescue_optimizer.py`
   - `failed_pool_rebuilder.py`
7. 汇总统计并输出 JSON/Excel

## 目录结构

```text
code/
├─ run_packing.py
├─ config.yaml
├─ README.md
├─ src/
│  ├─ config/
│  ├─ data/
│  ├─ geometry/
│  ├─ packing/
│  │  ├─ beam_search_packer.py
│  │  ├─ candidate_generator.py
│  │  ├─ direct_layer_packer.py
│  │  ├─ layer_pool_builder.py
│  │  ├─ placement_validator.py
│  │  ├─ pool_compactor.py
│  │  ├─ sanitizer.py
│  │  ├─ stacking_policy.py
│  │  └─ suction_planner.py
│  ├─ rescue/
│  ├─ main/
│  │  ├─ order_processor.py
│  │  ├─ output_formatter.py
│  │  ├─ pallet_packer.py
│  │  ├─ report_persister.py
│  │  ├─ result_formatter.py
│  │  └─ workflow.py
│  └─ utils/
└─ tests/
```

## 关键模块说明

| 模块 | 作用 |
|---|---|
| `src/main/pallet_packer.py` | 单分组主装箱流程、当前托盘补箱、失败托盘修复编排 |
| `src/packing/direct_layer_packer.py` | 整层确定性装箱与单箱居中兜底 |
| `src/packing/beam_search_packer.py` | 通用 beam search 装箱 |
| `src/packing/layer_pool_builder.py` | 整层候选池与配方构造 |
| `src/packing/stacking_policy.py` | 同尺寸重箱在下、倍数组层偏好等共享堆叠策略 |
| `src/packing/placement_validator.py` | 单次放置可行性校验 |
| `src/main/report_persister.py` | JSON 和 Excel 汇总持久化 |

## 主要配置

| 配置 | 文件 |
|---|---|
| 托盘目标指数 | `src/config/constants.py` |
| 最大箱间间隙 `6.0 mm` | `src/config/constants.py` |
| 小箱阈值检测参数 | `src/config/constants.py` |
| 托盘几何尺寸 | `config.yaml`、`src/config/pallet_config.py` |
| beam width / restart / candidate_limit | `config.yaml`、`src/config/algorithm_config.py` |
| 输出持久化方式 | `src/main/report_persister.py` |

## 运行

在仓库根目录执行：

```bash
python code/run_packing.py
```

运行完成后，默认会在 `output/` 下生成 JSON 与 Excel 两份结果文件。

## 测试

可按文件执行当前核心回归测试：

```bash
python code/tests/test_packing.py
python code/tests/test_main.py
python code/tests/test_rescue_pool.py
```

如环境中已安装 `pytest`，也可自行统一运行测试集。
