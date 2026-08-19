# DataSherlock Harness

> 面向数据仓库指标异常的自主诊断、验证与恢复执行框架  
> 项目核心是 **Agent Harness**。

---

## 1. 项目简介

在真实业务系统中，DAU、任务次数、转化率、付费用户数、会话时长等指标可能突然异常。

异常可能来自两类原因：

1. **真实业务变化**
   - 用户活跃下降
   - 某一端产品故障
   - 某地区用户流失
   - 新版本影响留存或转化

2. **数据系统故障**
   - 数据分区缺失
   - 批次重复导入
   - 数据延迟
   - 空值异常
   - Schema 变化
   - 字段漂移
   - 时区或单位错误
   - Join 过滤或 Join 膨胀
   - 指标口径发生变化

DataSherlock Harness 的目标是：

> 接收指标异常告警后，自主制定调查计划，调用受控 SQL 和数据质量工具，验证不同根因假设，输出有证据支撑的结论，并在人工审批后进行沙箱修复和回归验证。

## Current Implementation

当前分支已经实现或部分实现：

- Docker development skeleton、`data-init`、FastAPI `/health` 和 Streamlit shell；
- 可复现的 SaaS 正常数据生成器，包含用户、事件、订阅、实验分流、六个日指标和 Operational Metadata；
- DuckDB 本地数据库与 Parquet 输出；
- `config/metrics.yaml` 的强类型校验和可执行指标 SQL；
- `config/fault_catalog.yaml`、F01-F12 canonical taxonomy 和 machine-readable Ground Truth；
- F01-F12 最小故障注入器及目标 observable 测试；
- `IncidentState`、结构化 Planner 和严格只读 SQL Runner，包括 AST 校验、超时、行数限制、结构化响应和独立 JSONL audit；
- Planner 的 Pydantic 输入/输出契约、Tool Registry、工具名/参数/只读 SQL 语义校验、可审计 `PlannerRunResult`、分层重试与确定性 SQL 兜底计划；
- Provider-neutral `ModelClient`、异步 `OpenAIModelClient`、`MockModelClient`、调用元数据和可选真实 API smoke test；
- GitHub Actions CI 配置和完整单元测试。

指标配置中的 `timezone` 当前用于 IANA 时区校验；指标 SQL 仍以显式的
`CAST(event_time AS DATE)` 执行，统一的 timezone normalization 尚未接入。
F05 因此只验证明确的事件时间偏移和可观察的日边界变化。

仍未实现或仅有接口/文档准备：

- Data Quality Tools、Hypothesis Manager、Harness Graph、Checkpoint Runtime；
- Guardrail Runtime、Validators、完整 Fault Injector/Benchmark Runner、Approval Flow 和 Sandbox Repair；
- 完整 Streamlit 诊断界面、Benchmark 评测报告和生产数据接入。

## Quick Start

```bash
git clone https://github.com/zy8389/datasherlock-harness.git
cd datasherlock-harness
cp .env.example .env
docker compose up --build
```

服务地址：

- API：`http://localhost:8000`
- Frontend：`http://localhost:8501`

健康检查：

```bash
curl http://localhost:8000/health
```

## LLM Configuration

模型调用通过以下链路完成。Planner 不读取 API Key，也不创建 OpenAI SDK 客户端；Provider、模型和连接参数由环境配置及 Model Client factory 管理。

```text
Alert
  ↓
Planner
  ↓
ToolRegistry（当前只有 sql_query）
  ↓
ModelClient
  ↓
OpenAIModelClient
  ↓
OpenAI Responses API Structured Outputs
  ↓
InvestigationPlan
  ↓
Schema + Tool Semantic Validation
  ↓
PlannerRunResult
```

1. 复制 `.env.example` 为 `.env`；
2. 填写 `OPENAI_API_KEY` 和 `OPENAI_MODEL`；
3. `OPENAI_BASE_URL` 留空时使用 OpenAI SDK 默认 endpoint；需要兼容 endpoint 时再填写；
4. 使用 `LLM_TIMEOUT_SECONDS`、`LLM_MAX_RETRIES` 和 `LLM_RETRY_BASE_DELAY_SECONDS` 控制 Model Client 的传输层行为；
5. 普通 `pytest` 使用 `MockModelClient`，不会访问真实 API；
6. 只有同时设置 `RUN_LLM_INTEGRATION_TESTS=1`、`OPENAI_API_KEY` 和 `OPENAI_MODEL` 时，才运行 `tests/integration/test_openai_smoke.py`。

`LLM_MAX_RETRIES` 是 API/传输层重试次数，backoff 默认依次为 0.5s、1.0s、2.0s；Planner 自身的 Schema/语义修复重试由 `Planner(max_retries=...)` 单独控制，二者不会共用计数。正式调用建议使用 `Planner.arun()` / `Planner.run()`，从 `PlannerRunResult` 读取 `fallback_used`、`fallback_reason`、`model_result`、`planner_repair_count`、`transport_retry_count`、`provider` 和 `model`。即使最终 fallback，失败调用的 transport retry metadata 也会保留。

当前依赖范围为 `openai>=1.66,<3`；本地适配器测试使用 OpenAI Python SDK `2.54.0`。`responses.parse(..., text_format=...)` 与 `output_parsed` 是该依赖范围所要求的 Structured Outputs API 能力。

默认 Registry 只注册真实存在的 `sql_query`。当前尚未实现的数据质量、管道元数据、工具执行器和修复工具不会作为 Planner 的 Available Tool；fallback 也只生成交给 SQL Runner 执行的只读 SQL。默认 `pytest` 不访问真实模型，真实 smoke test 必须显式 opt-in。

Planner 当前采用 closed-set root-cause taxonomy：所有 `hypothesis.root_cause_type` 必须来自 `config/fault_catalog.yaml` 的 12 个 canonical fault types。它不会根据当前 metric 再额外缩小允许集合，也不会把 catalog 的 `expected_evidence` 或 benchmark ground truth 直接发送给模型。

---

## 2. 项目目标

第一阶段聚焦于一个明确任务：

> **SaaS 运营指标异常的根因诊断。**

系统需要完成以下闭环：

```text
异常告警
→ 创建调查任务
→ 生成调查计划
→ 调用 SQL / 数据质量工具
→ 更新并验证根因假设
→ 绑定证据
→ 输出修复建议
→ 人工审批
→ 沙箱修复
→ 回归验证
→ 生成事故报告
```

---

## 3. 为什么不是普通数据分析 Agent

普通数据分析 Agent 通常是：

```text
用户提问
→ LLM 生成 SQL
→ 执行 SQL
→ 输出自然语言解释
```

DataSherlock Harness 更关注 Agent 的可靠执行能力：

- 显式状态机
- 工具权限控制
- SQL 只读限制
- 最大步骤和成本限制
- 检查点保存
- 中断恢复
- 失败重试
- 重复操作检测
- 结果验证
- 证据绑定
- 人工审批
- 沙箱修复
- 全链路审计
- 自动 Benchmark

因此，本项目的技术重点是 **Harness 如何约束、调度和验证 Agent**。

---

## 4. MVP 范围

### 4.1 第一版必须完成

- SaaS 模拟数据生成器
- DuckDB 本地数据环境
- 指标语义配置
- 只读 SQL 执行器
- 数据质量检查工具
- 结构化 Planner
- 根因假设管理器
- 显式状态机
- 检查点与中断恢复
- Tool Guardrail
- 结果验证器
- 根因与证据验证器
- 12 类故障注入器
- Benchmark Runner
- Streamlit 演示界面
- Docker 化部署

### 4.2 第一版暂不做

- 通用 BI 问答
- 多行业同时接入
- 多 Agent 自由讨论
- 自动修改生产数据库
- 大规模企业数据仓库接入
- 复杂知识库 RAG
- 无人工审批的高风险修复

---

## 5. 核心状态机

```text
RECEIVED
→ TRIAGE
→ PLANNING
→ EXECUTING
→ VALIDATING
→ HYPOTHESIS_TESTING
→ ROOT_CAUSE_FOUND
→ FIX_PROPOSED
→ AWAITING_APPROVAL
→ SANDBOX_REPAIR
→ POST_VALIDATION
→ RESOLVED
```

异常终止状态：

```text
REJECTED
UNRESOLVED
BUDGET_EXCEEDED
TOOL_FAILED
VALIDATION_FAILED
```

每个状态都必须保存：

- 当前告警
- 调查计划
- 根因假设
- 已执行工具
- SQL 查询记录
- 证据
- 已驳回假设
- 重试次数
- Token 成本
- 当前结论
- 最终状态

---

## 6. 数据场景

第一阶段使用模拟 SaaS 平台数据。

### 6.1 users

```text
user_id
register_time
region
device_type
acquisition_channel
user_type
```

### 6.2 events

```text
event_id
user_id
event_time
event_name
session_id
device_type
duration_seconds
batch_id
app_version
app_build_number
```

事件类型示例：

```text
login
create_project
upload_file
run_ai_task
export_result
invite_member
```

### 6.3 subscriptions

```text
subscription_id
user_id
plan_type
start_time
end_time
subscription_status
monthly_fee
```

### 6.4 experiment_assignments

```text
experiment_id
user_id
variant
assigned_time
```

### 6.5 daily_metrics

```text
metric_date
daily_active_users
new_users
paid_users
ai_task_count
average_session_duration
conversion_rate
```

### 6.6 Operational Metadata

```text
pipeline_runs
partition_metadata
schema_snapshots
metric_versions
experiment_configs
```

这些表与五张业务/指标表一起写入 Parquet 和 DuckDB，用于提供独立的管道、分区、Schema、指标版本和实验配置证据。

建议首批数据规模：

- 用户：20,000
- 时间跨度：180 天
- 行为事件：约 100 万条
- 首批指标：6 个
- 首批故障案例：60 个

---

## 7. 首批故障类型

项目 Benchmark 需要覆盖以下 12 类故障：

| 编号 | 故障类型 | 典型表现 |
|---|---|---|
| F01 | 分区缺失 | 指标突然下降 |
| F02 | 批次重复 | 事件量异常增加 |
| F03 | 空值异常 | 去重用户数下降 |
| F04 | 数据延迟 | 当天下降、次日反弹 |
| F05 | 时区错误 | 小时峰值整体偏移 |
| F06 | 单位错误 | 会话时长异常放大 |
| F07 | Join 过滤 | 部分用户被错误过滤 |
| F08 | Join 膨胀 | 指标被重复放大 |
| F09 | 字段漂移 | 某类事件数量归零 |
| F10 | Schema 变化 | 数据任务执行失败 |
| F11 | 口径变化 | 新旧报表结果不一致 |
| F12 | A/B 分流异常 | 实验组比例严重失衡 |

每个故障案例必须包含：

```json
{
  "incident_id": "INC-001",
  "root_cause_type": "missing_partition",
  "affected_asset": "events_mobile_20260812",
  "affected_metric": "daily_active_users",
  "expected_root_cause": "移动端事件分区未完成写入",
  "expected_evidence": [
    "移动端事件量显著下降",
    "分区更新时间停留在前一天",
    "上游任务状态为失败"
  ]
}
```

---

## 8. Agent 可调用工具

### 8.1 当前已注册工具

```python
sql_query(sql)
```

`ToolRegistry` 当前只登记 `sql_query`，参数为一个 `sql` 字符串。Planner 生成计划时会动态注入该 Registry，并在接受模型结果前校验工具名、参数对象和 SQL 只读性。实际执行仍必须经过 `src/tools/sql_runner.py`，Planner 不直接操作 DuckDB。

### 8.2 后续规划中的工具（当前未注册）

```python
list_tables()
inspect_schema(table_name)
get_table_statistics(table_name)
get_partition_status(table_name, date)
compare_time_windows(metric, current_period, baseline_period)
drill_down_by_dimension(metric, dimension)
calculate_contribution(metric, dimension)
check_null_rate(table, column)
check_duplicate_rate(table, keys)
check_freshness(table)
detect_schema_drift(table)
detect_distribution_drift(table, column)
validate_join_cardinality(left_table, right_table, keys)
get_pipeline_status(job_id)
inspect_pipeline_logs(job_id)
get_data_lineage(metric)
list_upstream_tables(metric)
run_data_tests(model_name)
generate_sql_patch()
apply_patch_in_sandbox()
rerun_pipeline_in_sandbox()
validate_repaired_metric()
record_hypothesis()
record_evidence()
record_tool_result()
generate_incident_report()
```

以上名称来自后续架构规划，不代表当前可以被 Planner 使用；在对应实现和 Registry 注册完成前，模型不得生成这些名称。

---

## 9. 安全与 Guardrail

### 9.1 SQL 权限

`src/tools/sql_runner.py` 默认只允许单条：

```sql
SELECT
WITH
EXPLAIN
DESCRIBE
```

禁止：

```sql
DELETE
UPDATE
DROP
ALTER
TRUNCATE
INSERT
```

安全边界不依赖 SQL 前缀或关键词正则。执行器先使用 SQL AST 校验完整语句，
再使用 DuckDB 原生 parser 复核 statement type；`WITH` 的最终语句及
`EXPLAIN` / `EXPLAIN ANALYZE` 的内部语句都必须是只读查询。执行阶段使用
`read_only=True` 连接并关闭 DuckDB external access，阻止数据库写入以及
`read_csv`、`ATTACH` 等外部文件或网络访问。每次执行生成唯一 `query_id`。

写操作只能由独立的沙箱修复工具执行，并且必须经过人工审批，不能通过 SQL
Runner 绕过。

### 9.2 默认资源限制

```text
最大 Agent 轮数：20
最大 SQL 查询数：15
单次 SQL 超时：10 秒
最大返回行数：1000
最大 Python 执行时间：30 秒
最大修复重试：2 次
```

SQL Runner 会在超时后调用 DuckDB interrupt，并在返回第 1000 行后截断结果；
超时、校验失败和执行失败都携带同一次调用的 `query_id`。

### 9.3 根因输出要求

Agent 不允许只输出自然语言结论，必须返回结构化结果：

```json
{
  "root_cause_type": "missing_partition",
  "affected_asset": "events_mobile_20260812",
  "confidence": 0.94,
  "evidence": [
    {
      "evidence_id": "E07",
      "finding": "移动端事件数比过去7日均值低92.4%"
    },
    {
      "evidence_id": "E09",
      "finding": "移动端分区更新时间停留在前一天"
    }
  ],
  "proposed_fix": {
    "action": "rerun_partition",
    "target": "events_mobile_20260812"
  }
}
```

每个最终根因至少需要两条相互独立的证据。

---

## 10. 技术架构

```text
Streamlit / React
        ↓
FastAPI
        ↓
Agent Harness
├── Planner
├── State Machine
├── Hypothesis Manager
├── Tool Router
├── Guardrail
├── Validator
├── Checkpoint Manager
└── Audit Logger
        ↓
Tool Layer
├── SQL Runner
├── Data Quality Tools
├── Pipeline Inspector
└── Sandbox Repair
        ↓
Data Layer
├── DuckDB
├── Parquet
└── PostgreSQL / SQLite
        ↓
Benchmark Layer
├── Fault Injector
├── Ground Truth
├── Benchmark Runner
└── Evaluation Report
```

建议技术栈：

- Python 3.11+
- FastAPI
- DuckDB
- Pandas 或 Polars
- Pydantic
- LangGraph 或自研有限状态机
- PostgreSQL / SQLite
- Streamlit
- Docker Compose
- Pytest

---

## 11. 目标项目结构 / Planned Repository Structure

以下结构包含后续规划中的模块，不代表它们当前都已实现。

```text
datasherlock-harness/
├── app/
│   └── streamlit_app.py
├── config/
│   ├── metrics.yaml
│   ├── fault_catalog.yaml
│   ├── tools.yaml
│   └── settings.yaml
├── data/
│   ├── raw/
│   ├── processed/
│   └── benchmark/
├── benchmark/
│   ├── cases/
│   ├── ground_truth/
│   └── results/
├── docs/
│   ├── product/
│   ├── architecture/
│   ├── evaluation/
│   └── delivery/
├── experiments/
│   └── ablation/
├── src/
│   ├── agents/
│   │   └── planner.py
│   ├── llm/
│   │   ├── base.py
│   │   ├── models.py
│   │   ├── openai_client.py
│   │   ├── mock_client.py
│   │   └── factory.py
│   ├── benchmark/
│   │   ├── fault_injector.py
│   │   └── runner.py
│   ├── data/
│   │   └── generator.py
│   ├── harness/
│   │   ├── state.py
│   │   ├── graph.py
│   │   ├── checkpoint.py
│   │   ├── hypothesis.py
│   │   ├── guardrails.py
│   │   └── approval.py
│   ├── tools/
│   │   ├── sql_runner.py
│   │   ├── data_quality.py
│   │   └── pipeline_tools.py
│   ├── validators/
│   │   ├── sql_validator.py
│   │   └── root_cause_validator.py
│   └── api/
│       └── main.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── benchmark/
├── docker-compose.yml
├── pyproject.toml
├── .env.example
└── README.md
```

---

## 12. 团队分工建议

### Agent / Harness

负责：

- 状态机
- Planner
- Hypothesis Manager
- Checkpoint
- Guardrail
- Validator
- 人工审批
- Trace 与审计

### 数据工程

负责：

- 数据生成器
- DuckDB 表设计
- 指标语义配置
- 数据质量工具
- 故障注入器
- Ground Truth

### 后端与界面

负责：

- FastAPI
- 任务接口
- 状态查询
- Streamlit / React 页面
- 人工审批界面
- 报告展示
- Docker 部署

每个成员需要至少了解完整流程，不能只了解自己负责的模块。

---

## 13. 开发流程

### 开始任务前

1. 在 Notion 的“开发任务”中领取任务。
2. 将状态改为“进行中”。
3. 阅读验收标准和相关设计文档。
4. 新建 Git 分支。

分支命名：

```text
feature/sql-runner
feature/fault-injector
fix/checkpoint-recovery
docs/architecture
test/benchmark-runner
```

### 开发完成后

1. 完成本地测试。
2. 更新相关文档。
3. 提交 Pull Request。
4. 在 PR 中说明：
   - 做了什么
   - 为什么这样做
   - 如何测试
   - 是否影响接口
   - 是否新增配置
5. 通过审核后合并。
6. 更新 Notion 任务状态。

---

## 14. 代码规范

### 基本要求

- Python 使用类型标注
- 结构化数据使用 Pydantic
- 核心函数必须有 docstring
- 禁止在代码中硬编码密钥
- 禁止工具函数直接访问生产写权限
- SQL 必须经过统一执行器
- 每次工具调用必须生成 trace_id
- 每个异常必须有明确错误类型

### 提交信息

推荐格式：

```text
feat: add readonly SQL runner
fix: prevent duplicate tool calls after recovery
test: add missing partition benchmark cases
docs: update harness state machine
refactor: split validator from planner
```

---

## 15. 测试要求

### 单元测试

覆盖：

- SQL 白名单
- SQL 禁止操作
- Schema 漂移检测
- 空值与重复检查
- 状态转换
- 检查点序列化
- 假设去重
- 预算限制

### 集成测试

覆盖：

- 告警到根因完整流程
- 工具失败后的重试
- Agent 中断后的恢复
- 人工审批后的继续执行
- 沙箱修复后的指标回归

### Benchmark

至少比较：

```text
A：单次 Prompt
B：普通 ReAct Agent
C：状态图但无结果验证器
D：完整 DataSherlock Harness
```

---

## 16. 评测指标

### 任务结果

- Root Cause Top-1 Accuracy
- Root Cause Top-3 Recall
- Affected Asset Accuracy
- Evidence Completeness
- Repair Success Rate
- False Repair Rate

### Harness 能力

- Tool Success Rate
- Invalid SQL Rate
- Duplicate Action Rate
- Recovery Rate
- Unsafe Action Rate
- Average Tool Calls
- Average Diagnosis Time
- Average Token Cost

---

## 17. 第一阶段里程碑

### 第 1 周：数据环境

- 项目边界
- Docker 环境
- 数据生成器

### 第 2 周：工具层

- 指标语义配置
- SQL Runner
- 数据质量工具

### 第 3 周：Agent 规划

- IncidentState
- Planner
- Hypothesis Manager

### 第 4 周：Harness

- 状态机
- Checkpoint
- Guardrail

### 第 5 周：故障集

- 12 类故障注入器
- 60 个标准案例

### 第 6 周：验证器

- SQL Validator
- Root Cause Validator
- 人工审批

### 第 7 周：Benchmark

- Benchmark Runner
- 四组消融实验
- 失败案例分析

### 第 8 周：交付

- 演示界面
- README
- 架构文档
- 演示视频
- 简历材料

---

## 18. 新成员入组清单

新成员加入后，需要按顺序完成：

- [ ] 阅读本 README
- [ ] 阅读项目 Notion 页面
- [ ] 理解状态机和 12 类故障
- [ ] 拉取代码并启动 Docker 环境
- [ ] 成功生成一份模拟数据
- [ ] 成功执行一条只读 SQL
- [ ] 阅读一个 Ground Truth 案例
- [ ] 独立完成一个小任务
- [ ] 提交第一个 Pull Request
- [ ] 参加一次 Benchmark 结果复盘

---

## 19. 当前首要任务

项目成员优先完成：

1. 明确 MVP 边界与成功指标
2. 初始化代码仓库和 Docker 环境
3. 实现 SaaS 模拟数据生成器
4. 设计指标语义配置
5. 实现只读 SQL 执行器
6. 实现基础数据质量工具

在基础工具没有稳定前，不提前开发多 Agent、复杂 RAG 或自动修复。

---

## 20. 项目原则

1. **先可靠，再智能。**
2. **先做单 Agent Harness，再考虑多 Agent。**
3. **任何结论都必须绑定证据。**
4. **任何写操作都必须经过审批。**
5. **所有执行过程都必须可追踪、可恢复、可复现。**
6. **Benchmark 结果优先于演示效果。**
7. **不追求功能数量，优先完成一个闭环。**

---

## 21. 项目负责人

- 项目名称：DataSherlock Harness
- 当前阶段：MVP 开发
- 项目负责人：Zhang
- 项目管理：Notion
- 代码管理：Git / GitHub
- 主要沟通内容：任务进度、接口变更、失败案例、Benchmark 结果

---

> 新成员遇到不确定问题时，优先查看：
>
> 1. README  
> 2. Notion 对应任务页  
> 3. 架构文档  
> 4. Benchmark Ground Truth  
> 5. 再向负责人确认
