from __future__ import annotations

import json
import os
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
      [data-testid="stSidebar"] {background: #f5f7f6; border-right: 1px solid #d9dfdc;}
      h1, h2, h3 {letter-spacing: 0 !important; color: #17211d;}
      h1 {font-size: 2rem !important;}
      h2 {font-size: 1.35rem !important; margin-top: 1.4rem !important;}
      h3 {font-size: 1.05rem !important;}
      .ds-kicker {color: #407567; font-size: .76rem; font-weight: 700; text-transform: uppercase;}
      .ds-title {font-size: 2rem; line-height: 1.2; font-weight: 720; color: #17211d; margin: .2rem 0;}
      .ds-subtitle {color: #59655f; margin-bottom: 1rem;}
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
    st.error(f"API unavailable: {error}")
    st.caption(f"API_BASE_URL: {API_BASE_URL}")
    if st.button("Retry API", type="primary"):
        _rerun()
    st.code("docker compose up --build", language="bash")


def _status_flow(status: str) -> None:
    current_index = PRIMARY_FLOW.index(status) if status in PRIMARY_FLOW else -1
    nodes: list[str] = []
    for index, name in enumerate(PRIMARY_FLOW):
        css_class = (
            "current" if name == status else ("done" if index < current_index else "")
        )
        nodes.append(f'<span class="ds-node {css_class}">{name}</span>')
        if index < len(PRIMARY_FLOW) - 1:
            nodes.append('<span class="ds-arrow">›</span>')
    st.markdown(
        f'<div class="ds-flow">{"".join(nodes)}</div>',
        unsafe_allow_html=True,
    )
    if status in TERMINAL_ERRORS:
        st.error(f"Terminal state: {status}")


def _render_plan(incident: dict[str, Any]) -> None:
    st.subheader("Investigation Plan")
    plan_rows = [
        {
            "Step": item["step_id"],
            "Hypothesis": item["hypothesis_id"],
            "Purpose": item["purpose"],
            "Tool": item["tool"],
            "Expected evidence": "; ".join(item["expected_evidence"]),
            "Execution": item["execution_status"],
        }
        for item in incident["plan"]
    ]
    st.dataframe(plan_rows, use_container_width=True, hide_index=True)
    with st.expander("Read-only SQL investigation", expanded=False):
        for item in incident["plan"]:
            if item.get("sql"):
                st.caption(f'{item["step_id"]} · {item["purpose"]}')
                st.code(item["sql"], language="sql")


def _render_tool_trace(incident: dict[str, Any]) -> None:
    st.subheader("Tool Trace")
    trace_rows = [
        {
            "#": item["position"],
            "Tool": item["tool"],
            "Result": "SUCCESS" if item["success"] else "FAILURE",
            "Query ID": item.get("query_id") or "-",
            "Rows": item.get("row_count"),
            "Validation": (
                "passed"
                if item.get("validation", {}).get("passed")
                else ("failed" if item.get("validation") else "n/a")
            ),
            "Summary": item["result_summary"],
        }
        for item in incident["tool_trace"]
    ]
    st.dataframe(trace_rows, use_container_width=True, hide_index=True)
    for item in incident["tool_trace"]:
        label = (
            f'#{item["position"]} {item["tool"]} · '
            f'{"success" if item["success"] else "failure"}'
        )
        with st.expander(label, expanded=False):
            if item.get("error"):
                st.error(item["error"])
            st.json(
                {
                    "query_id": item.get("query_id"),
                    "validation": item.get("validation"),
                    "result": item.get("raw_result"),
                }
            )


def _render_root_cause(incident: dict[str, Any]) -> None:
    st.subheader("Root Cause & Evidence")
    root_cause = incident.get("root_cause")
    if not root_cause:
        st.info("No root cause has been authorized.")
        return
    left, right = st.columns([1, 2])
    with left:
        st.metric("Root cause", root_cause["root_cause_type"])
        st.metric("Confidence", f'{root_cause["confidence"]:.0%}')
        st.caption("Affected assets")
        st.write(", ".join(root_cause["affected_assets"]) or "-")
        st.caption("Independent evidence sources")
        badges = "".join(
            f'<span class="ds-source">{source}</span>'
            for source in root_cause["independent_source_types"]
        )
        st.markdown(badges, unsafe_allow_html=True)
    with right:
        evidence_rows = [
            {
                "Evidence ID": item["evidence_id"],
                "Source type": item["source_type"],
                "Finding": item["finding"],
                "Query ID": item.get("query_id") or "-",
            }
            for item in incident["evidence"]
        ]
        st.dataframe(evidence_rows, use_container_width=True, hide_index=True)
        for item in incident["evidence"]:
            with st.expander(
                f'{item["source_type"]} observation',
                expanded=False,
            ):
                st.json(item["observation"])


def _render_approval(incident: dict[str, Any]) -> None:
    proposal = incident.get("repair_proposal")
    if not proposal:
        return
    st.subheader("Human Approval")
    cols = st.columns(4)
    cols[0].metric("Action", proposal["action"])
    cols[1].metric("Risk", proposal["risk"].upper())
    cols[2].metric("Assets", len(proposal["affected_assets"]))
    cols[3].metric("Evidence bindings", len(proposal["evidence_bindings"]))
    st.write(proposal["rationale"])
    with st.expander("Proposal scope", expanded=False):
        st.json(proposal)
    st.markdown(
        '<div class="ds-callout">Repair runs in an isolated sandbox only. '
        "No production database is modified.</div>",
        unsafe_allow_html=True,
    )
    if not incident["can_approve"]:
        approval = incident.get("approval")
        if approval:
            st.success(
                f'{approval["outcome"].upper()} by {approval["reviewer"]} · '
                f'{approval.get("comment") or "No comment"}'
            )
        return
    with st.form("approval-form"):
        reviewer = st.text_input("Reviewer", value="demo-reviewer")
        comment = st.text_area("Optional comment", height=80)
        approve_col, reject_col, _ = st.columns([1, 1, 4])
        approve = approve_col.form_submit_button("Approve", type="primary")
        reject = reject_col.form_submit_button("Reject")
    if approve or reject:
        if reject and not comment.strip():
            st.error("A rejection comment is required.")
            return
        try:
            with st.spinner("Applying the approval decision..."):
                client.submit_approval(
                    incident["incident_id"],
                    reviewer=reviewer,
                    outcome="approved" if approve else "rejected",
                    comment=comment,
                )
        except DemoApiError as error:
            st.error(str(error))
            return
        _rerun()


def _render_repair_and_report(incident: dict[str, Any]) -> None:
    repair = incident.get("repair")
    if repair:
        st.subheader("Sandbox Repair")
        cols = st.columns(4)
        cols[0].metric("Run ID", repair["run_id"][:18] + "…")
        cols[1].metric("Action", repair["action"])
        cols[2].metric("Status", repair["status"].upper())
        cols[3].metric("Handler invocations", repair["handler_invocation_count"])
        if repair.get("changed_row_counts"):
            st.dataframe(
                [repair["changed_row_counts"]],
                use_container_width=True,
                hide_index=True,
            )
    validation = incident.get("post_validation")
    if validation:
        st.subheader("Post Validation")
        cols = st.columns(4)
        cols[0].metric("Status", validation["status"].upper())
        cols[1].metric("Before", f'{validation["observed_before"]:g}')
        cols[2].metric("After", f'{validation["observed_after"]:g}')
        cols[3].metric("Target met", "YES" if validation["target_met"] else "NO")
        st.write(validation["summary"])
    if incident["terminal"]:
        final_status = incident.get("final_status") or incident["status"]
        st.markdown(
            f'<div class="ds-terminal">Final status · {final_status}</div>',
            unsafe_allow_html=True,
        )
        st.subheader("Final Incident Report")
        report = incident.get("final_report")
        if report:
            report_json = json.dumps(report, indent=2, sort_keys=True)
            st.download_button(
                "Download final report JSON",
                data=report_json,
                file_name=f'datasherlock-{incident["incident_id"]}.json',
                mime="application/json",
            )
            with st.expander("Final report preview", expanded=False):
                st.json(report)


def _render_incident(incident: dict[str, Any]) -> None:
    case = incident["case"]
    top_left, top_right = st.columns([3, 1])
    with top_left:
        st.markdown(
            '<div class="ds-kicker">Canonical Incident</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="ds-title">{case["case_id"]} · {case["metric"]}</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            f'Incident {incident["incident_id"]} · deterministic smoke · 0 model calls'
        )
    with top_right:
        css = "terminal" if incident["terminal"] else "active"
        st.markdown(
            f'<div class="ds-status {css}">{incident["status"]}</div>',
            unsafe_allow_html=True,
        )
    alert_cols = st.columns(4)
    alert_cols[0].metric("Observed", f'{incident["alert"]["observed_value"]:g}')
    alert_cols[1].metric("Expected", f'{incident["alert"]["expected_value"]:g}')
    alert_cols[2].metric("Change", f'{incident["alert"]["change_rate"]:.1%}')
    alert_cols[3].metric("Severity", incident["alert"]["severity"].upper())
    st.subheader("Harness Status")
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
        '<div class="ds-kicker">Read-only Evaluation Artifact</div>',
        unsafe_allow_html=True,
    )
    st.header("Frozen Benchmark Snapshot")
    st.caption(f'{snapshot["run_id"]} · source commit {snapshot["source_commit"]}')
    st.warning(
        "Frozen historical benchmark. Not current-main accuracy. "
        "No post-PR20 real-model rerun has been performed."
    )
    rows = snapshot["rows"]
    table_rows = [
        {
            "Variant": row["display_name"],
            "Top-1": row["top_1"],
            "Top-3": row["top_3"],
            "Invalid SQL": row["invalid_sql_rate"],
            "Unsafe": row["unsafe_rate"],
            "Duplicate": row["duplicate_rate"],
            "Avg tools": row["avg_tool_calls"],
            "Avg SQL": row["avg_sql_calls"],
            "Mean latency ms": row["mean_latency_ms"],
            "Errors": row["errors"],
            "Timeouts": row["timeouts"],
            "Abstentions": row["abstentions"],
        }
        for row in rows
    ]
    st.dataframe(table_rows, use_container_width=True, hide_index=True)
    st.subheader("Top-k Accuracy")
    st.bar_chart(
        {
            row["display_name"]: {
                "Top-1": row["top_1"],
                "Top-3": row["top_3"],
            }
            for row in rows
        }
    )


st.markdown(
    '<div class="ds-kicker">Incident Diagnosis Runtime</div>',
    unsafe_allow_html=True,
)
st.title("DataSherlock Harness")
st.markdown(
    '<div class="ds-subtitle">Canonical deterministic incident demonstration</div>',
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
    st.subheader("Runtime")
    if health.get("status") == "ok":
        st.success("API healthy")
    else:
        st.warning(f'API {health.get("status", "unknown")}')
    st.caption("Deterministic smoke · 0 model calls")
    st.divider()
    st.subheader("Anomaly / Case Selection")
    case_ids = [item["case_id"] for item in cases]
    selected_case_id = st.selectbox("Canonical case", case_ids, index=0)
    selected_case = next(item for item in cases if item["case_id"] == selected_case_id)
    st.metric("Metric", selected_case["metric"])
    case_cols = st.columns(2)
    case_cols[0].metric("Observed", f'{selected_case["observed_value"]:g}')
    case_cols[1].metric("Expected", f'{selected_case["expected_value"]:g}')
    st.metric("Change", f'{selected_case["change_rate"]:.1%}')
    if not selected_case["interactive_supported"]:
        st.caption("Diagnosis demo not enabled for this case.")
    if st.button(
        "Start Diagnosis",
        type="primary",
        use_container_width=True,
        disabled=not selected_case["interactive_supported"],
    ):
        try:
            with st.spinner("Running the current Harness..."):
                started = client.start_incident(selected_case_id)
        except DemoApiError as error:
            st.error(str(error))
        else:
            _set_incident_id(started["incident_id"])
            _rerun()
    st.divider()
    st.subheader("Recent Incidents")
    incident_options = [""] + [item["incident_id"] for item in incidents]
    current_query_id = _query_incident_id()
    selected_index = (
        incident_options.index(current_query_id)
        if current_query_id in incident_options
        else 0
    )
    selected_incident_id = st.selectbox(
        "Persisted sessions",
        incident_options,
        index=selected_index,
        format_func=lambda value: (
            "Select an incident"
            if not value
            else next(
                (
                    f'{item["case_id"]} · {item["status"]} · {value[:8]}'
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

incident_tab, benchmark_tab = st.tabs(["Incident Demo", "Benchmark Snapshot"])
with incident_tab:
    incident_id = _query_incident_id()
    if incident_id:
        try:
            current_incident = client.get_incident(incident_id)
        except DemoApiError as error:
            st.error(str(error))
        else:
            _render_incident(current_incident)
    else:
        st.info("Select an interactive canonical case and start diagnosis.")
        supported = [item for item in cases if item["interactive_supported"]]
        st.dataframe(
            [
                {
                    "Case": item["case_id"],
                    "Metric": item["metric"],
                    "Observed": item["observed_value"],
                    "Expected": item["expected_value"],
                    "Change": item["change_rate"],
                }
                for item in supported
            ],
            use_container_width=True,
            hide_index=True,
        )
with benchmark_tab:
    _render_benchmark()
