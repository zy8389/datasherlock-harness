import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import llm.openai_client as openai_client_module
from agents.planner import (
    PLANNER_ALERT_EXAMPLES,
    Alert,
    InvestigationPlan,
    Planner,
    PlannerFallbackReason,
    PlannerInput,
    StructuredInvestigationPlan,
    build_fallback_plan,
    load_metric_context,
)
from config.model_settings import ModelSettings
from llm.base import (
    ModelAuthenticationError,
    ModelConfigurationError,
    ModelProviderError,
    ModelRateLimitError,
    ModelRequestError,
    ModelResponseError,
    ModelTimeoutError,
    ModelTransportError,
)
from llm.factory import create_model_client
from llm.mock_client import MockModelClient
from llm.models import ModelCallResult, ModelUsage
from llm.openai_client import OpenAIModelClient


def _plan() -> InvestigationPlan:
    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    context = load_metric_context(alert.metric)
    return build_fallback_plan(PlannerInput(alert=alert, metric_context=context))


class _FakeResponses:
    def __init__(self, response: object | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class _FakeClient:
    def __init__(self, responses: _FakeResponses):
        self.responses = responses


def _fake_response(parsed: object) -> SimpleNamespace:
    return SimpleNamespace(
        output_parsed=parsed,
        output_text="structured output",
        id="resp_test",
        _request_id="req_test",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7, total_tokens=18),
    )


def _client(
    responses: _FakeResponses,
    *,
    retries: int = 0,
    sleep_func=None,
    base_delay: float = 0.5,
) -> OpenAIModelClient:
    if sleep_func is None:
        async def no_sleep(_: float) -> None:
            return None

        sleep_func = no_sleep
    return OpenAIModelClient(
        ModelSettings(
            openai_api_key="test-key",
            openai_model="test-model",
            llm_max_retries=retries,
            llm_retry_base_delay_seconds=base_delay,
        ),
        client=_FakeClient(responses),
        sleep_func=sleep_func,
    )


def test_mock_model_client_and_planner_use_structured_result_without_network() -> None:
    model_client = MockModelClient(
        _plan(),
        model="mock-planner",
        usage=ModelUsage(input_tokens=100, output_tokens=40, total_tokens=140),
    )
    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    context = load_metric_context(alert.metric)

    planner = Planner(model_client)
    result = planner.plan(alert, context)

    assert result.incident_id == alert.incident_id
    assert len(result.hypotheses) == 5
    assert len(result.steps) == 5
    assert len(model_client.calls) == 1
    assert model_client.calls[0]["system_prompt"]
    assert model_client.calls[0]["user_prompt"]
    assert planner.last_model_result is not None


def test_planner_requests_strict_provider_schema_and_returns_canonical_plan() -> None:
    responses = _FakeResponses(response=_fake_response(_plan().model_dump(mode="json")))
    client = _client(responses)
    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    context = load_metric_context(alert.metric)

    result = Planner(client, max_retries=0).run(alert, context)

    assert result.fallback_used is False
    assert result.model_result is not None
    assert isinstance(result.model_result.parsed, InvestigationPlan)
    assert responses.calls[0]["text_format"] is StructuredInvestigationPlan


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ModelTimeoutError("timed out"), PlannerFallbackReason.MODEL_TIMEOUT),
        (ModelRateLimitError("rate limited"), PlannerFallbackReason.MODEL_RATE_LIMIT),
        (ModelTransportError("transport failed"), PlannerFallbackReason.MODEL_TRANSPORT_ERROR),
        (
            ModelAuthenticationError("authentication failed"),
            PlannerFallbackReason.MODEL_AUTHENTICATION_ERROR,
        ),
        (
            ModelRequestError("request rejected"),
            PlannerFallbackReason.MODEL_REQUEST_ERROR,
        ),
        (
            ModelProviderError("provider failed"),
            PlannerFallbackReason.MODEL_PROVIDER_ERROR,
        ),
    ],
)
def test_planner_run_records_provider_failure_fallback(
    error: Exception, reason: PlannerFallbackReason
) -> None:
    class FailingClient:
        async def generate_structured(self, **_: object):
            raise error

    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    context = load_metric_context(alert.metric)
    result = Planner(FailingClient()).run(alert, context)

    assert result.fallback_used is True
    assert result.fallback_reason == reason
    assert result.model_result is None


def test_planner_run_records_success_without_fallback() -> None:
    model_client = MockModelClient(_plan())
    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    context = load_metric_context(alert.metric)

    result = Planner(model_client).run(alert, context)

    assert result.fallback_used is False
    assert result.fallback_reason is None
    assert result.model_result is not None
    assert result.plan.incident_id == alert.incident_id


def test_model_call_result_preserves_provider_usage_latency_and_retry_metadata() -> None:
    responses = _FakeResponses(response=_fake_response(_plan()))
    result = asyncio.run(
        _client(responses).generate_structured(
            system_prompt="system",
            user_prompt="user",
            response_model=InvestigationPlan,
        )
    )

    assert result.provider == "openai"
    assert result.model == "test-model"
    assert result.parsed.incident_id == "INC-DAU-001"
    assert result.usage == ModelUsage(input_tokens=11, output_tokens=7, total_tokens=18)
    assert result.latency_ms >= 0
    assert result.request_id == "req_test"
    assert result.retry_count == 0
    assert result.transport_retry_count == 0
    assert result.planner_repair_count == 0


def test_openai_client_uses_responses_parse_with_pydantic_model() -> None:
    responses = _FakeResponses(response=_fake_response(_plan().model_dump(mode="json")))
    client = _client(responses)

    asyncio.run(
        client.generate_structured(
            system_prompt="system prompt",
            user_prompt="user prompt",
            response_model=InvestigationPlan,
        )
    )

    assert responses.calls[0]["model"] == "test-model"
    assert responses.calls[0]["instructions"] == "system prompt"
    assert responses.calls[0]["text_format"] is InvestigationPlan
    assert responses.calls[0]["input"] == [
        {"role": "user", "content": "user prompt"}
    ]


def test_openai_client_converts_timeout_and_provider_errors() -> None:
    timeout_responses = _FakeResponses(error=TimeoutError())
    with pytest.raises(ModelTimeoutError):
        asyncio.run(
            _client(timeout_responses, retries=1).generate_structured(
                system_prompt="system",
                user_prompt="user",
                response_model=InvestigationPlan,
            )
        )
    assert len(timeout_responses.calls) == 2

    provider_responses = _FakeResponses(error=RuntimeError("server unavailable"))
    with pytest.raises(ModelTransportError, match="provider call failed"):
        asyncio.run(
            _client(provider_responses).generate_structured(
                system_prompt="system",
                user_prompt="user",
                response_model=InvestigationPlan,
            )
        )
    assert len(provider_responses.calls) == 1


def test_openai_client_classifies_parse_validation_error_as_model_response_error() -> None:
    parse_error = ValidationError.from_exception_data(
        "InvestigationPlan",
        [
            {
                "type": "missing",
                "loc": ("incident_id",),
                "input": {},
                "ctx": {"error": "Field required"},
            }
        ],
    )
    responses = _FakeResponses(error=parse_error)

    with pytest.raises(ModelResponseError, match="Structured Output parsing failed"):
        asyncio.run(
            _client(responses).generate_structured(
                system_prompt="system",
                user_prompt="user",
                response_model=InvestigationPlan,
            )
        )


def test_openai_parse_validation_error_enters_planner_repair() -> None:
    parse_error = ValidationError.from_exception_data(
        "InvestigationPlan",
        [
            {
                "type": "missing",
                "loc": ("incident_id",),
                "input": {},
                "ctx": {"error": "Field required"},
            }
        ],
    )

    class SequenceResponses(_FakeResponses):
        def __init__(self) -> None:
            super().__init__()
            self.errors = [parse_error, None]

        async def parse(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            error = self.errors.pop(0)
            if error is not None:
                raise error
            return _fake_response(_plan().model_dump(mode="json"))

    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    context = load_metric_context(alert.metric)
    result = Planner(
        _client(SequenceResponses()),
        max_retries=1,
    ).run(alert, context)

    assert result.fallback_used is False
    assert result.model_result is not None
    assert result.planner_repair_count == 1


def test_openai_client_transport_retry_uses_injected_exponential_backoff() -> None:
    responses = _FakeResponses(error=TimeoutError())
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    with pytest.raises(ModelTimeoutError):
        asyncio.run(
            _client(responses, retries=2, sleep_func=fake_sleep).generate_structured(
                system_prompt="system",
                user_prompt="user",
                response_model=InvestigationPlan,
            )
        )

    assert delays == [0.5, 1.0]


class _FakeAPIStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status={status_code}")
        self.status_code = status_code


@pytest.mark.parametrize(
    ("status_code", "error_type", "call_count"),
    [
        (401, ModelAuthenticationError, 1),
        (403, ModelAuthenticationError, 1),
        (400, ModelRequestError, 1),
        (404, ModelRequestError, 1),
        (422, ModelRequestError, 1),
        (408, ModelTimeoutError, 3),
        (500, ModelProviderError, 3),
        (429, ModelRateLimitError, 3),
    ],
)
def test_openai_client_classifies_http_status_without_wrong_retries(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    error_type: type[Exception],
    call_count: int,
) -> None:
    monkeypatch.setattr(openai_client_module, "APIStatusError", _FakeAPIStatusError)
    responses = _FakeResponses(error=_FakeAPIStatusError(status_code))

    with pytest.raises(error_type):
        asyncio.run(
            _client(responses, retries=2).generate_structured(
                system_prompt="system",
                user_prompt="user",
                response_model=InvestigationPlan,
            )
        )

    assert len(responses.calls) == call_count


def test_timeout_failure_preserves_transport_retry_metadata_in_fallback() -> None:
    class FailingClient:
        async def generate_structured(self, **_: object):
            raise ModelTimeoutError(
                "timed out",
                transport_retry_count=2,
                provider="openai",
                model="test-model",
                latency_ms=12.5,
            )

    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    context = load_metric_context(alert.metric)
    result = Planner(FailingClient()).run(alert, context)

    assert result.fallback_used is True
    assert result.fallback_reason == PlannerFallbackReason.MODEL_TIMEOUT
    assert result.transport_retry_count == 2
    assert result.provider == "openai"
    assert result.model == "test-model"
    assert result.model_latency_ms == 12.5


def test_openai_client_rejects_malformed_structured_result() -> None:
    responses = _FakeResponses(response=_fake_response({"incident_id": "INC-DAU-001"}))

    with pytest.raises(ModelResponseError, match="Pydantic validation"):
        asyncio.run(
            _client(responses).generate_structured(
                system_prompt="system",
                user_prompt="user",
                response_model=InvestigationPlan,
            )
        )


def test_planner_repair_retry_is_separate_from_transport_retry() -> None:
    plan = _plan()
    calls = 0

    def response_factory(response_model: type[InvestigationPlan], _: str, __: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModelResponseError("semantic plan validation failed")
        return response_model.model_validate(plan)

    model_client = MockModelClient(response_factory)
    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    context = load_metric_context(alert.metric)
    planner = Planner(model_client, max_retries=1)

    result = planner.plan(alert, context)

    assert result == plan
    assert calls == 2
    assert planner.last_planner_repair_count == 1
    assert planner.last_model_result is not None
    assert planner.last_model_result.transport_retry_count == 0
    assert planner.last_model_result.planner_repair_count == 1
    assert planner.last_model_result.retry_count == 1


def test_model_response_error_transport_retry_is_preserved_after_repair() -> None:
    valid = _plan()
    calls = 0

    class ResponseErrorThenSuccessClient:
        async def generate_structured(self, **_: object) -> ModelCallResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ModelResponseError(
                    "structured output was invalid",
                    transport_retry_count=1,
                    provider="openai",
                    model="test-model",
                    latency_ms=10.0,
                )
            return ModelCallResult(
                provider="openai",
                model="test-model",
                parsed=valid,
                latency_ms=0.0,
                retry_count=0,
                transport_retry_count=0,
            )

    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    context = load_metric_context(alert.metric)
    result = Planner(ResponseErrorThenSuccessClient(), max_retries=1).run(
        alert, context
    )

    assert result.fallback_used is False
    assert result.planner_repair_count == 1
    assert result.transport_retry_count == 1
    assert result.provider == "openai"
    assert result.model == "test-model"
    assert result.model_result is not None
    assert result.model_result.transport_retry_count == 1
    assert result.model_result.planner_repair_count == 1
    assert result.model_result.retry_count == 2


def test_model_response_error_and_success_transport_retries_are_summed() -> None:
    valid = _plan()
    calls = 0

    class MixedResponseRetryClient:
        async def generate_structured(self, **_: object) -> ModelCallResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ModelResponseError(
                    "structured output was invalid",
                    transport_retry_count=1,
                    provider="openai",
                    model="test-model",
                    latency_ms=10.0,
                )
            return ModelCallResult(
                provider="openai",
                model="test-model",
                parsed=valid,
                latency_ms=20.0,
                retry_count=2,
                transport_retry_count=2,
            )

    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    context = load_metric_context(alert.metric)
    result = Planner(MixedResponseRetryClient(), max_retries=1).run(alert, context)

    assert result.fallback_used is False
    assert result.planner_repair_count == 1
    assert result.transport_retry_count == 3
    assert result.model_result is not None
    assert result.model_result.transport_retry_count == 3
    assert result.model_result.planner_repair_count == 1
    assert result.model_result.retry_count == 4


def test_response_error_retries_are_summed_with_final_timeout_fallback() -> None:
    calls = 0

    class ResponseErrorThenTimeoutClient:
        async def generate_structured(self, **_: object) -> ModelCallResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ModelResponseError(
                    "structured output was invalid",
                    transport_retry_count=1,
                    provider="openai",
                    model="test-model",
                    latency_ms=10.0,
                )
            raise ModelTimeoutError(
                "model request timed out",
                transport_retry_count=2,
                provider="openai",
                model="test-model",
                latency_ms=20.0,
            )

    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    context = load_metric_context(alert.metric)
    result = Planner(ResponseErrorThenTimeoutClient(), max_retries=1).run(
        alert, context
    )

    assert result.fallback_used is True
    assert result.fallback_reason == PlannerFallbackReason.MODEL_TIMEOUT
    assert result.transport_retry_count == 3
    assert result.planner_repair_count == 1
    assert result.provider == "openai"
    assert result.model == "test-model"


def test_transport_retry_and_planner_repair_counts_are_both_audited() -> None:
    valid = _plan()
    invalid = valid.model_copy(deep=True)
    invalid.steps[0].tool = "magic_tool"
    calls = 0

    class MixedRetryClient:
        async def generate_structured(self, **_: object) -> ModelCallResult:
            nonlocal calls
            calls += 1
            return ModelCallResult(
                provider="mock",
                model="mixed-retry",
                parsed=invalid if calls == 1 else valid,
                latency_ms=0,
                retry_count=1 if calls == 1 else 0,
                transport_retry_count=1 if calls == 1 else 0,
            )

    alert = Alert.model_validate(PLANNER_ALERT_EXAMPLES[0])
    context = load_metric_context(alert.metric)
    result = Planner(MixedRetryClient(), max_retries=1).run(alert, context)

    assert result.fallback_used is False
    assert result.planner_repair_count == 1
    assert result.model_result is not None
    assert result.model_result.transport_retry_count == 1
    assert result.model_result.planner_repair_count == 1
    assert result.model_result.retry_count == 2


def test_model_settings_does_not_require_secrets_and_normalizes_blank_url() -> None:
    settings = ModelSettings(
        model_provider="OPENAI",
        openai_api_key="",
        openai_model="test-model",
        openai_base_url="",
    )

    assert settings.model_provider == "openai"
    assert settings.openai_api_key is None
    assert settings.openai_base_url is None
    assert settings.openai_model == "test-model"


def test_unused_rate_limit_error_is_exposed_as_provider_neutral_type() -> None:
    assert issubclass(ModelRateLimitError, RuntimeError)


def test_provider_selection_stays_in_model_client_factory() -> None:
    with pytest.raises(ModelConfigurationError, match="unsupported model provider"):
        create_model_client(ModelSettings(model_provider="qwen"))
