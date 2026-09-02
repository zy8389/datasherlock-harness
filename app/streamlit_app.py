from __future__ import annotations

import json
import os
import re
from typing import Any

import streamlit as st

from app.api_client import DemoApiClient, DemoApiError

st.set_page_config(
    page_title="DataSherlock Harness",
    page_icon="🔎",
    layout="wide",
)

st.markdown(
    """
    <style>
      .block-container {max-width: 1480px; padding-top: 1.4rem; padding-bottom: 4rem;}
      [data-testid="stSidebar"] {background: var(--secondary-background-color); border-right: 1px solid rgba(128, 128, 128, .25);}
      [data-testid="stToolbar"], [data-testid="stDecoration"] {display: none;}
      h1, h2, h3 {letter-spacing: 0 !important; color: var(--text-color);}
      h1 {font-size: 2rem !important;}
      h2 {font-size: 1.35rem !important; margin-top: 1.4rem !important;}
      h3 {font-size: 1.05rem !important;}
      .ds-kicker {color: var(--primary-color); font-size: .76rem; font-weight: 700;}
      .ds-title {font-size: 2rem; line-height: 1.2; font-weight: 720; color: var(--text-color); margin: .2rem 0;}
      .ds-subtitle {color: var(--text-color); opacity: .72; margin-bottom: 1rem;}
      .ds-status {display: inline-flex; align-items: center; min-height: 2.2rem; padding: .35rem .7rem;
        border: 1px solid #cbd5d0; border-radius: 6px; font-weight: 700; background: #fff; color: #25332c;}
      .ds-status.active {background: #e8f5ef; border-color: #5f9b84; color: #155e45;}
      .ds-status.terminal {background: #ecf8f0; border-color: #4b956d; color: #17603d;}
      .ds-flow {display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; margin: .4rem 0 1rem;}
      .ds-node {padding: .25rem .42rem; border-radius: 4px; font-size: .7rem; border: 1px solid #d7dedb;
        color: #77817c; background: #fff; white-space: nowrap;}
      .ds-node.done {background: #edf6f2; color: #2b6652; border-color: #b9d7ca;}
      .ds-node.current {background: #176b51; color: #fff; border-color: #176b51; font-weight: 700;}
      .ds-arrow {color: #9aa39f; font-size: .72rem;}
      .ds-source {display: inline-block; margin: .15rem .35rem .15rem 0; padding: .2rem .45rem;
        border-radius: 4px; background: #eef4f1; border: 1px solid #cbdad3; color: #275c49; font-size: .78rem;}
      .ds-callout {border-left: 4px solid #d69e2e; padding: .65rem .8rem; background: #fff9e8; color: #5d4713;}
      .ds-terminal {border-left: 4px solid #24805d; padding: .7rem .85rem; background: #edf8f2; color: #16563d; font-weight: 700;}
      div[data-testid="stMetric"] {border-top: 2px solid #dce4e0; padding-top: .5rem;}
      div[data-testid="stDataFrame"] {border: 1px solid #dde3e0;}
    </style>
    """,
    unsafe_allow_html=True,
)

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
client = DemoApiClient(API_BASE_URL)

PRIMARY_FLOW = [
    "RECEIVED",
    "TRIAGE",
    "PLANNING",
    "EXECUTING",
    "VALIDATING",
    "HYPOTHESIS_TESTING",
    "ROOT_CAUSE_FOUND",
    "FIX_PROPOSED",
    "AWAITING_APPROVAL",
    "SANDBOX_REPAIR",
    "POST_VALIDATION",
    "RESOLVED",
]
TERMINAL_ERRORS = {
    "UNRESOLVED",
    "BUDGET_EXCEEDED",
    "TOOL_FAILED",
    "VALIDATION_FAILED",
    "REJECTED",
}

STATUS_LABELS = {
    "RECEIVED": "已接收",
    "TRIAGE": "分诊中",
    "PLANNING": "规划中",
    "EXECUTING": "执行中",
    "VALIDATING": "验证中",
    "HYPOTHESIS_TESTING": "假设检验中",
    "ROOT_CAUSE_FOUND": "已找到根因",
    "FIX_PROPOSED": "已提出修复方案",
    "AWAITING_APPROVAL": "等待审批",
    "SANDBOX_REPAIR": "沙箱修复中",
    "POST_VALIDATION": "修复后验证中",
    "RESOLVED": "已解决",
    "UNRESOLVED": "未解决",
    "BUDGET_EXCEEDED": "超出预算",
    "TOOL_FAILED": "工具执行失败",
    "VALIDATION_FAILED": "验证失败",
    "REJECTED": "已拒绝",
    "completed": "已完成",
    "pending": "待执行",
    "passed": "通过",
    "failed": "失败",
    "success": "成功",
    "succeeded": "成功",
    "running": "运行中",
    "approved": "已批准",
    "rejected": "已拒绝",
    "ok": "正常",
    "degraded": "服务降级",
    "unknown": "未知",
}

METRIC_LABELS = {
    "daily_active_users": "日活跃用户数",
    "new_users": "新增用户数",
    "paid_users": "付费用户数",
    "ai_task_count": "AI 任务数",
    "average_session_duration": "平均会话时长",
    "conversion_rate": "付费用户转化率",
}

ROOT_CAUSE_LABELS = {
    "missing_partition": "分区缺失",
    "duplicate_batch": "批次重复",
    "null_value_anomaly": "空值异常",
    "data_delay": "数据延迟",
    "timezone_error": "时区错误",
    "unit_error": "单位错误",
    "join_filter": "关联过滤错误",
    "join_explosion": "关联膨胀",
    "field_drift": "字段漂移",
    "schema_change": "模式变更",
    "metric_definition_change": "指标定义变更",
    "ab_split_anomaly": "A/B 分流异常",
}

SOURCE_LABELS = {
    "business_data": "业务数据",
    "operational_metadata": "运维元数据",
    "schema_metadata": "模式元数据",
    "metric_version": "指标版本",
    "experiment_config": "实验配置",
}

VALUE_LABELS = {
    **STATUS_LABELS,
    **METRIC_LABELS,
    **ROOT_CAUSE_LABELS,
    **SOURCE_LABELS,
    "rerun_partition": "重新运行分区",
    "low": "低",
    "medium": "中",
    "high": "高",
    "Single Prompt": "单提示词",
    "State Graph No Validator": "无验证器状态图",
    "Full Harness": "完整 Harness",
    "ready": "就绪",
    "missing": "缺失",
}

TEXT_LABELS = {
    "A target partition may be missing.": "目标分区可能缺失。",
    "The target data may have arrived late.": "目标数据可能延迟到达。",
    "Null values may distort the metric.": "空值可能导致指标失真。",
    "Inspect target-day business activity.": "检查目标日期的业务活动。",
    "Inspect operational partition metadata.": "检查运维分区元数据。",
    "Inspect business activity for the alert date.": "检查告警日期的业务活动。",
    "Inspect an independent operational or metadata signal.": (
        "检查一个独立的运维或元数据信号。"
    ),
    "target-day activity observation": "目标日期业务活动观测",
    "partition metadata observation": "分区元数据观测",
    "business activity observation": "业务活动观测",
    "independent metadata observation": "独立元数据观测",
    "one bounded candidate observation": "一项范围受限的候选观测",
    "Restore the confirmed missing events partition from the configured trusted repair source in an isolated sandbox.": (
        "在隔离沙箱中，从已配置的可信修复源恢复已确认缺失的 events 分区。"
    ),
    "Tool call succeeded.": "工具调用成功。",
    "Tool call failed.": "工具调用失败。",
}


def _display_value(value: Any) -> str:
    text = str(value)
    return VALUE_LABELS.get(text, text)


def _display_text(value: Any) -> str:
    text = str(value)
    if text in TEXT_LABELS:
        return TEXT_LABELS[text]
    match = re.fullmatch(r"Read-only query returned (\d+) row\(s\)\.", text)
    if match:
        return f"只读查询返回 {match.group(1)} 行。"
    match = re.fullmatch(r"Inspect one bounded path for (.+)\.", text)
    if match:
        return f"检查{_display_value(match.group(1))}的一条范围受限路径。"
    match = re.fullmatch(r"Business activity query returned (.+)\.", text)
    if match:
        return f"业务活动查询返回 {match.group(1)}。"
    match = re.fullmatch(r"partition_metadata reports (.+)\.", text)
    if match:
        return f"partition_metadata 报告：{match.group(1)}。"
    match = re.fullmatch(
        r"(.+) changed from (.+) to (.+); expected (.+) within (.+)\. "
        r"Target partition and configured checks are healthy\.",
        text,
    )
    if match:
        return (
            f"{_display_value(match.group(1))} 从 {match.group(2)} 变为 "
            f"{match.group(3)}；预期值为 {match.group(4)}，允许误差 "
            f"{match.group(5)}。目标分区及配置的检查项均正常。"
        )
    return text


def _display_error(error: Any) -> str:
    text = str(error)
    prefix = "interactive diagnosis is not enabled for "
    if text.startswith(prefix):
        return f"案例 {text.removeprefix(prefix)} 尚未启用交互式诊断。"
    return _display_text(text)


def _query_incident_id() -> str | None:
    value = st.query_params.get("incident_id")
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _set_incident_id(incident_id: str) -> None:
    st.query_params["incident_id"] = incident_id


def _rerun() -> None:
    st.rerun()


def _api_failure(error: DemoApiError) -> None:
    st.error(f"API 不可用：{_display_error(error)}")
    st.caption(f"API_BASE_URL: {API_BASE_URL}")
    if st.button("重试 API", type="primary"):
        _rerun()
    st.code("docker compose up --build", language="bash")


def _status_flow(status: str) -> None:
    current_index = PRIMARY_FLOW.index(status) if status in PRIMARY_FLOW else -1
    nodes: list[str] = []
    for index, name in enumerate(PRIMARY_FLOW):
        css_class = (
            "current" if name == status else ("done" if index < current_index else "")
        )
        nodes.append(
            f'<span class="ds-node {css_class}">{_display_value(name)}</span>'
        )
        if index < len(PRIMARY_FLOW) - 1:
            nodes.append('<span class="ds-arrow">›</span>')
    st.markdown(
        f'<div class="ds-flow">{"".join(nodes)}</div>',
        unsafe_allow_html=True,
    )
    if status in TERMINAL_ERRORS:
        st.error(f"终止状态：{_display_value(status)}")


def _render_plan(incident: dict[str, Any]) -> None:
    st.subheader("调查计划")
    plan_rows = [
        {
            "步骤": item["step_id"],
            "假设": item["hypothesis_id"],
            "目的": _display_text(item["purpose"]),
            "工具": item["tool"],
            "预期证据": "；".join(
                _display_text(value) for value in item["expected_evidence"]
            ),
            "执行状态": _display_value(item["execution_status"]),
        }
        for item in incident["plan"]
    ]
    st.dataframe(plan_rows, use_container_width=True, hide_index=True)
    with st.expander("只读 SQL 调查", expanded=False):
        for item in incident["plan"]:
            if item.get("sql"):
                st.caption(f'{item["step_id"]} · {_display_text(item["purpose"])}')
                st.code(item["sql"], language="sql")


def _render_tool_trace(incident: dict[str, Any]) -> None:
    st.subheader("工具调用轨迹")
    trace_rows = [
        {
            "#": item["position"],
            "工具": item["tool"],
            "结果": "成功" if item["success"] else "失败",
            "查询 ID": item.get("query_id") or "-",
            "行数": item.get("row_count"),
            "验证": (
                "通过"
                if item.get("validation", {}).get("passed")
                else ("失败" if item.get("validation") else "不适用")
            ),
            "摘要": _display_text(item["result_summary"]),
        }
        for item in incident["tool_trace"]
    ]
    st.dataframe(trace_rows, use_container_width=True, hide_index=True)
    for item in incident["tool_trace"]:
        label = (
            f'#{item["position"]} {item["tool"]} · '
            f'{"成功" if item["success"] else "失败"}'
        )
        with st.expander(label, expanded=False):
            if item.get("error"):
                st.error(_display_error(item["error"]))
            st.json(
                {
                    "query_id": item.get("query_id"),
                    "validation": item.get("validation"),
                    "result": item.get("raw_result"),
                }
            )


def _render_root_cause(incident: dict[str, Any]) -> None:
    st.subheader("根因与证据")
    root_cause = incident.get("root_cause")
    if not root_cause:
        st.info("尚未授权任何根因结论。")
        return
    left, right = st.columns([1, 2])
    with left:
        st.metric("根因", _display_value(root_cause["root_cause_type"]))
        st.metric("置信度", f'{root_cause["confidence"]:.0%}')
        st.caption("受影响资产")
        st.write(", ".join(root_cause["affected_assets"]) or "-")
        st.caption("独立证据来源")
        badges = "".join(
            f'<span class="ds-source">{_display_value(source)}</span>'
            for source in root_cause["independent_source_types"]
        )
        st.markdown(badges, unsafe_allow_html=True)
    with right:
        evidence_rows = [
            {
                "证据 ID": item["evidence_id"],
                "来源类型": _display_value(item["source_type"]),
                "发现": _display_text(item["finding"]),
                "查询 ID": item.get("query_id") or "-",
            }
            for item in incident["evidence"]
        ]
        st.dataframe(evidence_rows, use_container_width=True, hide_index=True)
        for item in incident["evidence"]:
            with st.expander(
                f'{_display_value(item["source_type"])}观测详情',
                expanded=False,
            ):
                st.json(item["observation"])


def _render_approval(incident: dict[str, Any]) -> None:
    proposal = incident.get("repair_proposal")
    if not proposal:
        return
    st.subheader("人工审批")
    cols = st.columns(4)
    cols[0].metric("动作", _display_value(proposal["action"]))
    cols[1].metric("风险", _display_value(proposal["risk"]))
    cols[2].metric("资产数", len(proposal["affected_assets"]))
    cols[3].metric("证据绑定数", len(proposal["evidence_bindings"]))
    st.write(_display_text(proposal["rationale"]))
    with st.expander("方案范围", expanded=False):
        st.json(proposal)
    st.markdown(
        '<div class="ds-callout">修复只在隔离沙箱中运行，'
        "不会修改生产数据库。</div>",
        unsafe_allow_html=True,
    )
    if not incident["can_approve"]:
        approval = incident.get("approval")
        if approval:
            st.success(
                f'{_display_value(approval["outcome"])} · 审批人：'
                f'{approval["reviewer"]} · {approval.get("comment") or "无备注"}'
            )
        return
    with st.form("approval-form"):
        reviewer = st.text_input("审批人", value="演示审批人")
        comment = st.text_area("备注（可选；拒绝时必填）", height=80)
        approve_col, reject_col, _ = st.columns([1, 1, 4])
        approve = approve_col.form_submit_button("批准", type="primary")
        reject = reject_col.form_submit_button("拒绝")
    if approve or reject:
        if reject and not comment.strip():
            st.error("拒绝时必须填写备注。")
            return
        try:
            with st.spinner("正在提交审批决定..."):
                client.submit_approval(
                    incident["incident_id"],
                    reviewer=reviewer,
                    outcome="approved" if approve else "rejected",
                    comment=comment,
                )
        except DemoApiError as error:
            st.error(_display_error(error))
            return
        _rerun()


def _render_repair_and_report(incident: dict[str, Any]) -> None:
    repair = incident.get("repair")
    if repair:
        st.subheader("沙箱修复")
        cols = st.columns(4)
        cols[0].metric("运行 ID", repair["run_id"][:18] + "…")
        cols[1].metric("动作", _display_value(repair["action"]))
        cols[2].metric("状态", _display_value(repair["status"]))
        cols[3].metric("处理器调用次数", repair["handler_invocation_count"])
        if repair.get("changed_row_counts"):
            st.dataframe(
                [repair["changed_row_counts"]],
                use_container_width=True,
                hide_index=True,
            )
    validation = incident.get("post_validation")
    if validation:
        st.subheader("修复后验证")
        cols = st.columns(4)
        cols[0].metric("状态", _display_value(validation["status"]))
        cols[1].metric("修复前", f'{validation["observed_before"]:g}')
        cols[2].metric("修复后", f'{validation["observed_after"]:g}')
        cols[3].metric("达到目标", "是" if validation["target_met"] else "否")
        st.write(_display_text(validation["summary"]))
    if incident["terminal"]:
        final_status = incident.get("final_status") or incident["status"]
        st.markdown(
            f'<div class="ds-terminal">最终状态 · {_display_value(final_status)}</div>',
            unsafe_allow_html=True,
        )
        st.subheader("最终事件报告")
        report = incident.get("final_report")
        if report:
            report_json = json.dumps(report, indent=2, sort_keys=True)
            st.download_button(
                "下载最终报告 JSON",
                data=report_json,
                file_name=f'datasherlock-{incident["incident_id"]}.json',
                mime="application/json",
            )
            with st.expander("最终报告预览", expanded=False):
                st.json(report)


def _render_incident(incident: dict[str, Any]) -> None:
    case = incident["case"]
    top_left, top_right = st.columns([3, 1])
    with top_left:
        st.markdown(
            '<div class="ds-kicker">标准事件</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="ds-title">{case["case_id"]} · '
            f'{_display_value(case["metric"])}</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            f'事件 {incident["incident_id"]} · 确定性冒烟测试 · 0 次模型调用'
        )
    with top_right:
        css = "terminal" if incident["terminal"] else "active"
        st.markdown(
            f'<div class="ds-status {css}">'
            f'{_display_value(incident["status"])}</div>',
            unsafe_allow_html=True,
        )
    alert_cols = st.columns(4)
    alert_cols[0].metric("观测值", f'{incident["alert"]["observed_value"]:g}')
    alert_cols[1].metric("预期值", f'{incident["alert"]["expected_value"]:g}')
    alert_cols[2].metric("变化率", f'{incident["alert"]["change_rate"]:.1%}')
    alert_cols[3].metric("严重级别", _display_value(incident["alert"]["severity"]))
    st.subheader("Harness 状态")
    _status_flow(incident["status"])
    _render_plan(incident)
    _render_tool_trace(incident)
    _render_root_cause(incident)
    _render_approval(incident)
    _render_repair_and_report(incident)


def _render_benchmark() -> None:
    try:
        snapshot = client.benchmark_snapshot()
    except DemoApiError as error:
        _api_failure(error)
        return
    st.markdown(
        '<div class="ds-kicker">只读评估产物</div>',
        unsafe_allow_html=True,
    )
    st.header("冻结的基准快照")
    st.caption(f'{snapshot["run_id"]} · 来源提交 {snapshot["source_commit"]}')
    st.warning(
        "这是冻结的历史基准结果，并非当前 main 分支的准确率。"
        "PR #20 之后尚未重新运行真实模型。"
    )
    rows = snapshot["rows"]
    table_rows = [
        {
            "变体": _display_value(row["display_name"]),
            "Top-1": row["top_1"],
            "Top-3": row["top_3"],
            "无效 SQL 率": row["invalid_sql_rate"],
            "不安全操作率": row["unsafe_rate"],
            "重复操作率": row["duplicate_rate"],
            "平均工具调用数": row["avg_tool_calls"],
            "平均 SQL 调用数": row["avg_sql_calls"],
            "平均延迟（毫秒）": row["mean_latency_ms"],
            "错误数": row["errors"],
            "超时数": row["timeouts"],
            "弃答数": row["abstentions"],
        }
        for row in rows
    ]
    st.dataframe(table_rows, use_container_width=True, hide_index=True)
    st.subheader("Top-k 准确率")
    st.bar_chart(
        {
            _display_value(row["display_name"]): {
                "Top-1": row["top_1"],
                "Top-3": row["top_3"],
            }
            for row in rows
        }
    )


st.markdown(
    '<div class="ds-kicker">事件诊断运行台</div>',
    unsafe_allow_html=True,
)
st.title("DataSherlock Harness")
st.markdown(
    '<div class="ds-subtitle">标准确定性事件诊断演示</div>',
    unsafe_allow_html=True,
)

try:
    health = client.health()
    cases = client.list_cases()
    incidents = client.list_incidents()
except DemoApiError as api_error:
    _api_failure(api_error)
    st.stop()

with st.sidebar:
    st.subheader("运行状态")
    if health.get("status") == "ok":
        st.success("API 运行正常")
    else:
        st.warning(f'API：{_display_value(health.get("status", "unknown"))}')
    st.caption("确定性冒烟测试 · 0 次模型调用")
    st.divider()
    st.subheader("异常案例选择")
    case_ids = [item["case_id"] for item in cases]
    selected_case_id = st.selectbox("标准案例", case_ids, index=0)
    selected_case = next(item for item in cases if item["case_id"] == selected_case_id)
    st.metric("指标", _display_value(selected_case["metric"]))
    case_cols = st.columns(2)
    case_cols[0].metric("观测值", f'{selected_case["observed_value"]:g}')
    case_cols[1].metric("预期值", f'{selected_case["expected_value"]:g}')
    st.metric("变化率", f'{selected_case["change_rate"]:.1%}')
    if not selected_case["interactive_supported"]:
        st.caption("此案例尚未启用交互式诊断演示。")
    if st.button(
        "开始诊断",
        type="primary",
        use_container_width=True,
        disabled=not selected_case["interactive_supported"],
    ):
        try:
            with st.spinner("正在运行当前 Harness..."):
                started = client.start_incident(selected_case_id)
        except DemoApiError as error:
            st.error(_display_error(error))
        else:
            _set_incident_id(started["incident_id"])
            _rerun()
    st.divider()
    st.subheader("最近事件")
    incident_options = [""] + [item["incident_id"] for item in incidents]
    current_query_id = _query_incident_id()
    selected_index = (
        incident_options.index(current_query_id)
        if current_query_id in incident_options
        else 0
    )
    selected_incident_id = st.selectbox(
        "已保存会话",
        incident_options,
        index=selected_index,
        format_func=lambda value: (
            "选择一个事件"
            if not value
            else next(
                (
                    f'{item["case_id"]} · {_display_value(item["status"])} · '
                    f'{value[:8]}'
                    for item in incidents
                    if item["incident_id"] == value
                ),
                value,
            )
        ),
    )
    if selected_incident_id and selected_incident_id != current_query_id:
        _set_incident_id(selected_incident_id)
        _rerun()

incident_tab, benchmark_tab = st.tabs(["事件演示", "基准快照"])
with incident_tab:
    incident_id = _query_incident_id()
    if incident_id:
        try:
            current_incident = client.get_incident(incident_id)
        except DemoApiError as error:
            st.error(_display_error(error))
        else:
            _render_incident(current_incident)
    else:
        st.info("请选择支持交互的标准案例并开始诊断。")
        supported = [item for item in cases if item["interactive_supported"]]
        st.dataframe(
            [
                {
                    "案例": item["case_id"],
                    "指标": _display_value(item["metric"]),
                    "观测值": item["observed_value"],
                    "预期值": item["expected_value"],
                    "变化率": item["change_rate"],
                }
                for item in supported
            ],
            use_container_width=True,
            hide_index=True,
        )
with benchmark_tab:
    _render_benchmark()
