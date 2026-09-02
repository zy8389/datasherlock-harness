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
      "tool": "sql_query",
      "arguments": {
        "sql": "SELECT table_name, partition_value, row_count, status FROM partition_metadata WHERE table_name = 'events'"
      },
      "expected_evidence": ["目标分区不存在或行数为零"],
      "stop_condition": "若分区正常，则降低 H01 优先级并继续下一条假设。"
    }
  ]
}
```

输出必须包含 3–5 个不同的候选假设，步骤最多 10 个。每个步骤都必须声明目的、假设引用、工具、参数、预期证据和停止条件；步骤引用不存在的假设、重复 ID、未知工具、错误参数或修复工具都会被拒绝。

## Tool Registry

Planner 通过依赖注入获得 `ToolRegistry`。Registry 只描述当前真实存在的工具，不执行工具，也不包含动态插件发现：

```python
from tools.registry import ToolRegistry, build_default_tool_registry

registry = build_default_tool_registry()
assert registry.names() == (
    "sql_query",
    "check_null_rate",
    "check_duplicate_rate",
    "check_freshness",
    "detect_schema_drift",
    "detect_distribution_drift",
)
planner = Planner(create_model_client(), tool_registry=registry)
```

当前默认 Registry 注册六个只读工具：

```text
sql_query
check_null_rate
check_duplicate_rate
check_freshness
detect_schema_drift
detect_distribution_drift
```

`sql_query` 的 `sql` 必须是单条只读 SQL。五个 Data Quality 工具也通过
`ToolExecutor` 和同一个只读 SQL Runner 执行。管道写工具和修复工具不会出现在调查
Prompt 的 `Available tools` 中。模型输出经过 Pydantic Schema 后，还会由 Planner 校验
canonical `root_cause_type`、工具是否存在、参数是否符合 Registry JSON Schema、工具是否
只读、工具是否属于该 fault family，以及 `sql_query` 是否通过 SQL Runner 的 AST/native
parser 校验。

`root_cause_type` 采用 closed-set 约束，但允许 F01-F12 中任意 canonical fault type；允许集合
每次从 `config/fault_catalog.yaml` 读取，不在 Planner 中维护第二份 Literal 列表，也不会
根据当前 metric 进一步缩小集合。PR #20 后，正式 Prompt 会提供 applicable fault family 的
`evidence_source_types`、`verification_fields` 和 `expected_evidence`，但明确将它们定义为
candidate diagnostic objectives，而不是观测事实或 Ground Truth 答案。Case ID、expected
root cause、injection 参数、source seed ID 和 Ground Truth 对象仍不会进入 Planner 输入。

每个 proposed hypothesis 必须至少有一个调查 step。对于 catalog 声明了两个或更多
`evidence_source_types` 的候选，Planner 必须用 distinct steps 覆盖所有声明来源。一个混合
多个来源的 SQL、未知 asset 或多个同类型来源都不能伪装成独立证据覆盖。详细规则见
[Planner Evidence-Source Coverage](planner_evidence_coverage.md)。

## 调用方式

Planner 的正式调用方式注入 `ModelClient`，不绑定具体 Provider SDK：

```python
from agents.planner import Planner, load_metric_context
from llm.factory import create_model_client

metric_context = load_metric_context("daily_active_users")
result = Planner(create_model_client()).run(alert, metric_context)
state.plan = [result.plan.model_dump(mode="json")]
```

正式链路为：

```text
Alert
  ↓
Planner
  ↓
ToolRegistry
  ↓
Available tools prompt
  ↓
ModelClient
  ↓
OpenAIModelClient
  ↓
AsyncOpenAI.responses.parse(..., text_format=InvestigationPlan)
  ↓
InvestigationPlan
  ↓
Pydantic + semantic validation
  ↓
PlannerRunResult
```

OpenAI Structured Outputs 先由 SDK 根据 Pydantic 模型解析，Model Client 再执行一次 Pydantic 校验并返回统一的 `ModelCallResult`，其中记录 provider、model、parsed、usage、latency、request_id 和重试计数。Planner 不读取 API Key、不创建 SDK 客户端，也不处理 OpenAI 原始 Response 类型。

`LLM_MAX_RETRIES` 只属于 Model Client 的传输重试；Planner `max_retries` 只属于 Schema/业务计划修复重试。模型输出被判定为 `ModelResponseError` 或语义校验失败时，Planner 才执行修复重试；超时、限流和传输错误在 Model Client 内部重试后统一转换，并由 Planner 使用带有 `fallback_used` 与 `fallback_reason` 的兜底结果收束。Transport retry 和 Planner repair retry 分别记录在 `ModelCallResult.transport_retry_count` 与 `ModelCallResult.planner_repair_count`，总数记录在 `retry_count`。

`responses.parse()` 直接抛出的 Pydantic `ValidationError` 属于 Structured Output response error，会转换为 `ModelResponseError` 并进入 Planner repair；它不会被误归类为 transport error。终态 fallback 的 `PlannerRunResult` 仍保留 `transport_retry_count`、`provider`、`model` 和可用的 `model_latency_ms`。

为保持第一版离线测试兼容，旧的 `generate(prompt) -> str` 注入形式仍由同步 `Planner.plan()` 临时支持；新代码应使用 `ModelClient`，异步调用推荐 `await Planner(...).arun(...)`。`aplan()` / `plan()` 仍只返回 `InvestigationPlan` 以兼容旧调用方，正式 Harness 应使用 `arun()` / `run()` 获取完整 `PlannerRunResult`。模型输出不再由正式 ModelClient 路径自行 `json.loads`。

## PlannerRunResult 与 fallback

`PlannerRunResult` 明确区分模型生成和确定性 fallback：

```python
class PlannerRunResult(BaseModel):
    plan: InvestigationPlan
    model_result: ModelCallResult[InvestigationPlan] | None
    fallback_used: bool
    fallback_reason: PlannerFallbackReason | None
    planner_repair_count: int
    transport_retry_count: int
    model_latency_ms: float | None
    provider: str | None
    model: str | None
```

例如：

```text
OpenAI timeout
→ ModelTimeoutError
→ PlannerRunResult(fallback_used=true, fallback_reason=MODEL_TIMEOUT)
```

模型第一次生成 `magic_tool` 时：

```text
结构化 Schema 通过
→ Tool semantic validation 失败
→ Planner repair retry
→ 第二次合法
→ fallback_used=false, planner_repair_count=1
```

模型不可用时，fallback 不再生成不存在的工具；当前 fallback 的每一步都是 `sql_query`，SQL 会复用 SQL Runner 的只读校验边界。

Provider 错误使用 provider-neutral 类型归一化：401/403 为 `ModelAuthenticationError`，400/404/422 为 `ModelRequestError`，408 为 `ModelTimeoutError`，429 为 `ModelRateLimitError`，5xx 为可重试的 `ModelProviderError`，连接失败为 `ModelTransportError`。认证和请求错误不做无意义重试。

F05 `timezone_error` 的 fallback 查询通过 `events e JOIN users u ON e.user_id = u.user_id` 获取 `u.region`，再按 region 和 `EXTRACT(HOUR FROM e.event_time)` 聚合，仍由 SQL Runner 做只读校验。

## 稳定性样例

仓库中的 `PLANNER_ALERT_EXAMPLES` 和 `tests/unit/test_planner.py` 覆盖三条告警：

- `daily_active_users` 下降 24%；
- `ai_task_count` 上升 40%；
- `conversion_rate` 下降 35%。

测试验证三条样例都能产生 3–5 个候选、有限步骤和完整步骤字段，并验证非法输出重试及连续失败后的确定性回退。
