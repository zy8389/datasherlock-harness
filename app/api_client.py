"""Small HTTP JSON client used by the Streamlit presentation layer."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class DemoApiError(RuntimeError):
    """Normalized API transport or response error for user-facing handling."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DemoApiClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def health(self) -> dict[str, Any]:
        return self._request("/health")

    def list_cases(self) -> list[dict[str, Any]]:
        return self._request("/demo/cases")["cases"]

    def list_incidents(self) -> list[dict[str, Any]]:
        return self._request("/demo/incidents")["incidents"]

    def start_incident(self, case_id: str) -> dict[str, Any]:
        return self._request(
            "/demo/incidents",
            method="POST",
            payload={"case_id": case_id},
        )

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        return self._request(f"/demo/incidents/{incident_id}")

    def submit_approval(
        self,
        incident_id: str,
        *,
        reviewer: str,
        outcome: str,
        comment: str,
    ) -> dict[str, Any]:
        return self._request(
            f"/demo/incidents/{incident_id}/approval",
            method="POST",
            payload={
                "reviewer": reviewer,
                "outcome": outcome,
                "comment": comment or None,
            },
        )

    def benchmark_snapshot(self) -> dict[str, Any]:
        return self._request("/demo/benchmark")

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc.read())
            raise DemoApiError(detail, status_code=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DemoApiError(f"API 请求失败：{exc}") from exc
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise DemoApiError("API 返回了无效的 JSON") from exc
        if not isinstance(decoded, dict):
            raise DemoApiError("API 返回的 JSON 不是对象")
        return decoded


def _error_detail(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "API 请求被拒绝"
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
        return payload["detail"]
    return "API 请求被拒绝"


__all__ = ["DemoApiClient", "DemoApiError"]
