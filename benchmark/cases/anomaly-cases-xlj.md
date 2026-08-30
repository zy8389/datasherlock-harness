# DataSherlock F01-F12 标准案例

本文件是可读案例索引；机器可执行口径以 `config/fault_catalog.yaml`、
`benchmark/ground_truth/F01-001.yaml` 至 `F12-001.yaml` 和生成的
`benchmark/cases/Fxx-yyy.yaml` 为准。每个故障族有一份 canonical case 说明，并生成 5 个
确定性变体，共 12 类故障 x 5 个可复现案例 = 60 个 manifest。每个案例都明确告警现象、
样例查询方向、业务证据、独立元数据/配置证据和 canonical 根因。

## 证据查询约定

- 业务证据通过 `sql_query` 查询 `events`、`users`、`subscriptions` 或
  `experiment_assignments`。
- 独立证据通过 `sql_query` 查询 `partition_metadata`、`pipeline_runs`、
  `schema_snapshots`、`metric_versions` 或 `experiment_configs`。
- `check_freshness`、`check_duplicate_rate`、`check_null_rate`、
  `detect_schema_drift` 和 `detect_distribution_drift` 是受注册表约束的只读补充检查：
  F01 使用 freshness，F03 使用 null-rate，F09 使用 distribution-drift，F10 使用
  schema-drift。F02/F04/F08/F12 使用 `sql_query`，因为当前 duplicate-rate 缺少事故范围，
  freshness 无法识别部分延迟，而 distribution-drift 无法比较 F12 所需的故障前基线；它们不替代
  metadata-dependent case 所需的独立证据路径。
- F01/F04/F05/F10/F11/F12 的 `evidence_paths` 是验收合同；不要用两个业务查询冒充独立证据。
- 所有查询必须是只读 SQL，并由 SQL Runner 校验。

## F01 `missing_partition`

- **Case / metric**: `F01-001`, `daily_active_users`
- **告警现象**: 目标日 DAU 下降，目标日 Android 事件缺失，邻近日期仍有数据。
- **注入**: 删除 Android 目标分区。
- **业务查询**: 按目标日期和 `device_type = 'android'` 统计 `events` 行数与去重用户数。
- **业务证据**: 目标日 Android 事件为零或显著减少。
- **独立证据**: `partition_metadata` 的目标 Android 分区 `row_count = 0`、`status = missing`。
- **标准根因**: `missing_partition`。

## F02 `duplicate_batch`

- **Case / metric**: `F02-001`, `ai_task_count`
- **告警现象**: AI task count 上升，原始事件行数和重复 event_id 增加。
- **注入**: 按比例重复 `run_ai_task` 事件批次。
- **业务查询**: 按目标日检查总行数、`COUNT(DISTINCT event_id)`、重复 event_id 数量和 `ai_task_count`。
- **业务证据**: 原始行数上升且 event_id 重复，任务次数增加。
- **独立证据**: `pipeline_runs` 记录 `duplicate_batch` 运行异常。
- **标准根因**: `duplicate_batch`。

## F03 `null_value_anomaly`

- **Case / metric**: `F03-001`, `daily_active_users`
- **告警现象**: DAU 下降，事件总行数稳定，但移动端 `user_id` 空值率上升。
- **注入**: 将目标日移动端事件的 `user_id` 置空。
- **业务查询**: 统计目标日总行数、`user_id IS NULL` 行数和有效去重用户数。
- **业务证据**: 行数稳定、空值率上升、DAU 下降。
- **独立证据**: `pipeline_runs` 记录 `null_value_anomaly`。
- **标准根因**: `null_value_anomaly`。

## F04 `data_delay`

- **Case / metric**: `F04-001`, `daily_active_users`
- **告警现象**: 目标日 Android 事件减少，次日对应事件 rebound，DAU 目标日下降。
- **注入**: 将 60% 的目标日 Android 事件延迟一天。
- **业务查询**: 对目标日和次日按设备统计事件数。
- **业务证据**: 目标日减少、次日增加。
- **独立证据**: `pipeline_runs` 目标分区 `status = delayed` 或 `error_type = data_delay`。
- **标准根因**: `data_delay`。

## F05 `timezone_error`

- **Case / metric**: `F05-001`, `daily_active_users`
- **告警现象**: CN 日期边界附近小时分布改变，目标日 DAU 发生偏移。
- **注入**: 将 CN 边界事件偏移 8 小时，并生成目标日期的新 metric version。
- **业务查询**: 按 CN 用户和小时统计目标日事件分布。
- **业务证据**: 小时分布发生固定偏移，目标日 DAU 改变。
- **独立证据**: `metric_versions` 记录 `daily_active_users` `UTC -> Asia/Shanghai`。
- **标准根因**: `timezone_error`。

## F06 `unit_error`

- **Case / metric**: `F06-001`, `average_session_duration`
- **告警现象**: 平均会话时长出现千倍放大或极端 outlier。
- **注入**: 将部分 `duration_seconds` 乘以 1000。
- **业务查询**: 统计目标日时长的均值、最小值、最大值和分布分位数。
- **业务证据**: 时长分布整体偏移，最大值出现千倍异常，平均值上升。
- **独立证据**: 对照 `schema_snapshots.schema_json` 和字段单位说明，确认字段语义未支持该数量级。
- **标准根因**: `unit_error`。

## F07 `join_filter`

- **Case / metric**: `F07-001`, `daily_active_users`
- **告警现象**: DAU 下降，未订阅用户被错误过滤。
- **注入**: 给 DAU 查询增加错误的 subscription inner join。
- **业务查询**: 比较 `COUNT(DISTINCT events.user_id)` 与 join 后用户数，并按用户类型拆分。
- **业务证据**: join 后用户数下降，free 用户损失更明显。
- **独立证据**: `metric_versions.query` / `definition_hash` 记录异常 inner join。
- **标准根因**: `join_filter`。

## F08 `join_explosion`

- **Case / metric**: `F08-001`, `ai_task_count`
- **告警现象**: AI task count 和 joined event rows 膨胀。
- **注入**: 重复实验 assignment，并使用 assignment join 计算任务数。
- **业务查询**: 统计 assignment 每用户行数、joined rows 和 distinct event_id。
- **业务证据**: assignment 一人多行，joined rows 超过 distinct event_id。
- **独立证据**: `metric_versions.query` / `definition_hash` 记录异常 join 口径。
- **标准根因**: `join_explosion`。

## F09 `field_drift`

- **Case / metric**: `F09-001`, `ai_task_count`
- **告警现象**: `run_ai_task` 频率下降，`execute_ai_task` 新值出现，总事件量基本稳定。
- **注入**: 将部分 `event_name` 从 `run_ai_task` 改为 `execute_ai_task`。
- **业务查询**: 按 event_name 统计目标日事件频率。
- **业务证据**: 旧值下降、新值出现、总事件量保持稳定。
- **独立证据**: 结合 `events.app_build_number` 或 `schema_snapshots` 定位新版本字段值。
- **标准根因**: `field_drift`。

## F10 `schema_change`

- **Case / metric**: `F10-001`, `daily_active_users`
- **告警现象**: 目标日事件未物化，DAU 下降，pipeline/partition failed。
- **注入**: 将 `app_build_number` 从 `BIGINT` 改为 `VARCHAR`，使兼容性检查失败。
- **业务查询**: 统计目标日 events 行数和 DAU。
- **业务证据**: 目标日业务数据未物化或显著减少。
- **独立证据**: `schema_snapshots` 显示 `BIGINT -> VARCHAR`，`pipeline_runs.error_type = schema_change`，分区状态为 failed。
- **标准根因**: `schema_change`。

## F11 `metric_definition_change`

- **Case / metric**: `F11-001`, `daily_active_users`
- **告警现象**: raw events 行数稳定，但 DAU 下降。
- **注入**: 将 DAU SQL 收窄为只统计 core task 事件。
- **业务查询**: 分别查询目标日 raw event count 和 daily_metrics 的 DAU。
- **业务证据**: raw event count 不变，DAU 下降。
- **独立证据**: `metric_versions` 新版本的 version、query、definition_hash 均变化。
- **标准根因**: `metric_definition_change`。

## F12 `ab_split_anomaly`

- **Case / metric**: `F12-001`, `conversion_rate`
- **告警现象**: 实验分组从 50/50 变为 20/80，conversion rate 上升。
- **注入**: 改变 assignment allocation，保留原 treatment 用户且保证用户唯一。
- **业务查询**: 按 variant 统计 assignment，并比较目标日 conversion_rate。
- **业务证据**: assignment 分布变化，conversion rate 达到 catalog 的最小 effect threshold。
- **独立证据**: `experiment_configs` 新版本记录 `control_ratio = 0.20`、`treatment_ratio = 0.80`。
- **标准根因**: `ab_split_anomaly`。

## 60 个可复现案例与运行验收

- `benchmark/cases/variants.yaml` 是 60 个 variant 的唯一参数来源；
  `python -m benchmark.case_generator --check` 可验证已提交 manifest 没有漂移。
- `F01-001` 至 `F12-001` 是每族默认变体；`Fxx-002` 至 `Fxx-005` 固定变化 baseline seed、
  metric date 或注入强度，同时保持 catalog 的方向、effect 与 evidence contract。
- `tests/benchmark/test_case_generation.py` 与
  `tests/benchmark/test_case_materialization.py` 验证 60 个 manifest 均可确定性生成、物化，
  且满足 effect 与 Evidence Contract。
- F01/F11 的真实 runtime gate 在
  `tests/benchmark/test_full_runtime_e2e_gate.py`：它通过 `HarnessGraph` 执行 Planner、
  Tool Executor、只读 SQL、guardrail、checkpoint/resume、证据绑定与 authoritative
  root-cause validation；测试输入不携带 Ground Truth 答案。
- `benchmark.runner` 在同一组 60-case manifest 上持久化每个案例的结果与 trace，并输出评测摘要。

这些范围已经落地在当前代码库；生产数据接入仍不在本地 Benchmark 的范围内。
