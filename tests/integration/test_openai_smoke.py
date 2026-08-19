"""Opt-in real OpenAI smoke test; never runs in the default test suite."""

from __future__ import annotations

import asyncio
import os

import pytest

from agents.planner import Alert, Planner, load_metric_context
from config.model_settings import ModelSettings
from llm.factory import create_model_client

_ENABLED = (
    os.getenv("RUN_LLM_INTEGRATION_TESTS") == "1"
    and bool(os.getenv("OPENAI_API_KEY"))
    and bool(os.getenv("OPENAI_MODEL"))
)

pytestmark = pytest.mark.skipif(
    not _ENABLED,
    reason="set RUN_LLM_INTEGRATION_TESTS=1, OPENAI_API_KEY and OPENAI_MODEL to opt in",
)


def test_openai_planner_returns_investigation_plan() -> None:
    alert = Alert(
        incident_id="INC-SMOKE-001",
        metric="daily_active_users",
        observed_at="2026-01-30",
        expected_value=10000,
        observed_value=7000,
        change_rate=-0.30,
        severity="medium",
    )
    context = load_metric_context(alert.metric)
    planner = Planner(create_model_client(ModelSettings()), max_retries=1)

    plan = asyncio.run(planner.aplan(alert, context))

    assert plan.incident_id == alert.incident_id
    assert 3 <= len(plan.hypotheses) <= 5
    assert plan.steps
