# YE 指标与故障映射

这份文档是 `config/metrics.yaml`、`config/fault_catalog.yaml` 和
`benchmark/ground_truth/*.yaml` 的可读索引。可执行口径仍以 YAML 为准；文档用于
审查正常定义、常见异常、验证字段、工具和标准根因是否一致。

## 指标语义

| 指标 | 正常定义 | 主要异常表现 | 关键验证字段 | 工具 |
| --- | --- | --- | --- | --- |
| `daily_active_users` | 目标日期发生至少一个事件的去重用户数 | 日活骤降、设备/地区消失、次日回补 | `events.event_time`, `events.user_id`, `events.device_type`, `users.region` | `sql_query`, `check_null_rate`, `check_freshness`, `detect_schema_drift` |
| `new_users` | `register_time` 落在目标日期的去重用户数 | 注册归零、注册日期偏移、地区/设备消失 | `users.user_id`, `users.register_time`, `users.region`, `users.device_type` | `sql_query` |
| `paid_users` | 目标日期处于有效订阅生命周期的去重用户数 | 订阅有效但未计数、已结束订阅仍计数、超过 DAU | `subscriptions.user_id`, `subscriptions.start_time`, `subscriptions.end_time`, `subscriptions.subscription_status` | `sql_query` |
| `ai_task_count` | `event_name = 'run_ai_task'` 的事件数 | 重复导入或 Join 膨胀导致上升、字段漂移导致下降 | `events.event_id`, `events.event_name`, `events.event_time`, `experiment_assignments.user_id` | `sql_query`, `detect_distribution_drift` |
| `average_session_duration` | 按用户和 session 汇总事件时长后取平均值，单位为秒 | 单位放大/缩小、极端 outlier、整体偏离历史范围 | `events.duration_seconds`, `events.session_id`, `events.user_id`, `events.event_time` | `sql_query` |
| `conversion_rate` | 目标日期活跃且当天开始付费订阅的去重用户数 / DAU | 实验分流后转化率变化、分子/分母归零、脱离 DAU 变化 | `events.user_id`, `events.event_time`, `subscriptions.user_id`, `subscriptions.start_time`, `experiment_assignments.variant` | `sql_query` |

## F01-F12 映射

| Case | 标准根因 | 影响指标 | 注入策略 | 业务证据 | 独立/配置证据 | 关键验证字段 | 诊断工具 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| F01 | `missing_partition` | `daily_active_users` | 删除 Android 目标分区 | 目标日 Android 事件缺失 | `partition_metadata` 行数为 0、状态 `missing` | `events.event_time`, `events.device_type`, `partition_metadata.partition_value`, `partition_metadata.row_count`, `partition_metadata.status` | `sql_query`, `check_freshness` |
| F02 | `duplicate_batch` | `ai_task_count` | 重复 AI task batch 行 | event_id 重复、原始行数增加 | `pipeline_runs` 标记 duplicate batch | `events.event_id`, `events.event_name`, `events.event_time`, `pipeline_runs.error_type` | `sql_query` |
| F03 | `null_value_anomaly` | `daily_active_users` | 将移动端 user_id 置空 | 行数稳定但有效 DAU 下降 | `pipeline_runs` 标记 null anomaly | `events.user_id`, `events.event_time`, `events.device_type`, `pipeline_runs.error_type` | `sql_query`, `check_null_rate` |
| F04 | `data_delay` | `daily_active_users` | Android 事件延迟一天 | 目标日下降、次日 rebound | `pipeline_runs` / partition freshness 为 delayed | `events.event_time`, `events.device_type`, `partition_metadata.status`, `pipeline_runs.status`, `pipeline_runs.error_type` | `sql_query` |
| F05 | `timezone_error` | `daily_active_users` | 移动 CN 日期边界事件并改变时区版本 | CN 小时分布改变、目标日 DAU 改变 | `metric_versions` `UTC -> Asia/Shanghai` | `events.event_time`, `events.user_id`, `users.region`, `metric_versions.timezone`, `metric_versions.effective_at` | `sql_query` |
| F06 | `unit_error` | `average_session_duration` | 放大 duration_seconds | 时长分布出现千倍 outlier | `schema_snapshots` 与字段单位上下文 | `events.duration_seconds`, `events.session_id`, `events.event_time`, `schema_snapshots.schema_json` | `sql_query` |
| F07 | `join_filter` | `daily_active_users` | 给 DAU 增加错误 subscription inner join | 去重用户数下降、free 用户受损 | `metric_versions` query/hash 改变 | `events.user_id`, `subscriptions.user_id`, `subscriptions.start_time`, `metric_versions.query` | `sql_query` |
| F08 | `join_explosion` | `ai_task_count` | 重复 assignment 并放大 Join | assignment 一人多行、joined task 行数上升 | `metric_versions` query/hash 改变 | `events.event_id`, `events.user_id`, `experiment_assignments.user_id`, `metric_versions.query` | `sql_query` |
| F09 | `field_drift` | `ai_task_count` | `run_ai_task -> execute_ai_task` | 旧事件频率下降、新值出现 | schema/版本字段可用于识别新值 | `events.event_name`, `events.app_build_number`, `events.event_time`, `schema_snapshots.schema_json` | `sql_query`, `detect_distribution_drift` |
| F10 | `schema_change` | `daily_active_users` | `app_build_number BIGINT -> VARCHAR` | 目标日数据未物化、DAU 下降 | `schema_snapshots`、pipeline/partition failed | `schema_snapshots.schema_json`, `partition_metadata.status`, `pipeline_runs.error_type` | `sql_query`, `detect_schema_drift` |
| F11 | `metric_definition_change` | `daily_active_users` | DAU SQL 过滤为 core task | raw event 行数稳定但 DAU 下降 | `metric_versions` version/hash/query 改变 | `events.event_time`, `events.event_name`, `metric_versions.version`, `metric_versions.definition_hash`, `metric_versions.query` | `sql_query` |
| F12 | `ab_split_anomaly` | `conversion_rate` | 实验分流 `50/50 -> 20/80` | assignment 分布和 conversion 改变 | `experiment_configs` 新版本与 20/80 | `experiment_assignments.variant`, `experiment_configs.version`, `experiment_configs.control_ratio`, `experiment_configs.treatment_ratio`, `subscriptions.start_time` | `sql_query` |

## 运行约束

- `sql_query` 与五个已注册的 Data Quality 工具均为只读；SQL Runner 对 SQL 做 AST、只读、超时和行数限制校验，Data Quality 工具在相同受控执行边界内生成结构化观察。
- `diagnostic_tools` 是 Catalog/Metric 的可执行能力映射，必须由 `ToolRegistry` 注册，并且 Catalog 中每个故障工具必须属于其受影响指标的工具集合。它不会被放入 Planner 的指标语义提示词，防止诊断细节或配置约束改变 Planner 的受控输入边界；Planner 在模型输出后按候选根因的 Catalog 映射校验每一步工具，拒绝越界工具。
- F01、F04、F05、F10、F11、F12 需要业务证据加独立元数据/配置证据；这由 Ground Truth 的 `evidence_paths` 和 `validate_expected_evidence()` 强制执行。
- F01/F11 的完整 runtime E2E gate 已覆盖 `inject -> alert -> Planner -> Tool Executor -> SQL Runner -> business + independent evidence -> checkpoint/resume -> Evidence Validator -> authoritative root cause`。运行输入不携带 Ground Truth 答案；Ground Truth 只在最终评测时使用。
- `benchmark/cases/variants.yaml` 生成并锁定 60 个 F01-F12 manifest；Case Generator、materialization tests 和 Benchmark Runner 负责可复现的注入、评测结果与 trace。
- Data Quality Tools、HarnessGraph、guardrail、checkpoint/resume 与 Benchmark Runner 已存在于当前代码库。生产数据接入仍不在本地 Benchmark 的范围内。
