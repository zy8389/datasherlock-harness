# Planner 设计

Planner 负责把一条结构化异常告警和指标上下文转换为“待验证的调查计划”。它不输出最终根因，也不执行修复动作。

## 输入

```json
{
  "alert": {
    "incident_id": "INC-DAU-001",
    "metric": "daily_active_users",
    "observed_at": "2026-01-30",
    "expected_value": 10000,
    "observed_value": 7600,
    "change_rate": -0.24,
    "severity": "high"
  },
  "metric_context": {
    "metric_id": "daily_active_users",
    "description": "Distinct users with at least one event on the metric date.",
    "source_tables": ["events"],
    "time_column": "event_time",
    "entity_column": "user_id"
  }
}
```

`load_metric_context()` 可以直接从 `config/metrics.yaml` 读取指标语义，避免 Planner 自己维护第二套指标口径。告警中的 `metric` 必须和上下文中的 `metric_id` 一致。

## 输出

`InvestigationPlan` 是严格的 Pydantic Schema，额外字段会被拒绝：

```json
{
  "incident_id": "INC-DAU-001",
  "hypotheses": [
    {
      "hypothesis_id": "H01",
      "root_cause_type": "missing_partition",
      "description": "待检查目标分区是否缺失。",
      "initial_confidence": 0.45
    },
    {
      "hypothesis_id": "H02",
      "root_cause_type": "data_delay",
      "description": "待检查数据是否延迟到达。",
      "initial_confidence": 0.35
    },
    {
      "hypothesis_id": "H03",
      "root_cause_type": "null_value_anomaly",
      "description": "待检查关键实体字段空值率是否突增。",
      "initial_confidence": 0.25
    }
  ],
  "steps": [
    {
      "step_id": "S01",
      "purpose": "检查目标日期的分区状态。",
      "hypothesis_id": "H01",
      "tool": "get_partition_status",
      "arguments": {"table": "events", "date": "2026-01-30"},
      "expected_evidence": ["目标分区不存在或行数为零"],
      "stop_condition": "若分区正常，则降低 H01 优先级并继续下一条假设。"
    }
  ]
}
```

输出必须包含 3–5 个不同的候选假设，步骤最多 10 个。每个步骤都必须声明目的、假设引用、工具、参数、预期证据和停止条件；步骤引用不存在的假设、重复 ID 或修复工具都会被拒绝。

## 调用方式

Planner 的正式调用方式注入 `ModelClient`，不绑定具体 Provider SDK：

```python
from agents.planner import Planner, load_metric_context
from llm.factory import create_model_client

metric_context = load_metric_context("daily_active_users")
plan = Planner(create_model_client()).plan(alert, metric_context)
state.plan = [plan.model_dump(mode="json")]
```

正式链路为：

```text
Planner
  ↓
ModelClient
  ↓
OpenAIModelClient
  ↓
AsyncOpenAI.responses.parse(..., text_format=InvestigationPlan)
  ↓
InvestigationPlan
```

OpenAI Structured Outputs 先由 SDK 根据 Pydantic 模型解析，Model Client 再执行一次 Pydantic 校验并返回统一的 `ModelCallResult`，其中记录 provider、model、parsed、usage、latency、request_id 和重试计数。Planner 不读取 API Key、不创建 SDK 客户端，也不处理 OpenAI 原始 Response 类型。

`LLM_MAX_RETRIES` 只属于 Model Client 的传输重试；Planner `max_retries` 只属于 Schema/业务计划修复重试。模型输出被判定为 `ModelResponseError` 时，Planner 才执行修复重试；超时、限流和传输错误在 Model Client 内部重试后统一转换，并由 Planner 使用兜底计划收束。

为保持第一版离线测试兼容，旧的 `generate(prompt) -> str` 注入形式仍由同步 `Planner.plan()` 临时支持；新代码应使用 `ModelClient`，异步调用推荐 `await Planner(...).aplan(...)`。模型输出不再由正式 Planner 路径自行 `json.loads`。

## 稳定性样例

仓库中的 `PLANNER_ALERT_EXAMPLES` 和 `tests/unit/test_planner.py` 覆盖三条告警：

- `daily_active_users` 下降 24%；
- `ai_task_count` 上升 40%；
- `conversion_rate` 下降 35%。

测试验证三条样例都能产生 3–5 个候选、有限步骤和完整步骤字段，并验证非法输出重试及连续失败后的确定性回退。
