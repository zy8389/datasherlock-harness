import json

import pytest

from api.auth import (
    APPROVE_REPAIR_PERMISSION,
    ApprovalAuthenticationConfigurationError,
    ApprovalAuthenticationError,
    ApprovalAuthenticator,
    ApprovalAuthorizationError,
)


def _identities(*, permissions: list[str]) -> dict[str, str]:
    return {
        "INCIDENT_APPROVAL_IDENTITIES": json.dumps(
            [
                {
                    "token": "configured-test-token",
                    "subject": "approver@example.com",
                    "permissions": permissions,
                    "identity_source": "test_bearer",
                }
            ]
        )
    }


def test_authenticator_returns_the_configured_approval_principal() -> None:
    authenticator = ApprovalAuthenticator.from_environment(
        _identities(permissions=[APPROVE_REPAIR_PERMISSION])
    )

    principal = authenticator.require_approval("Bearer configured-test-token")

    assert principal.subject == "approver@example.com"
    assert principal.identity_source == "test_bearer"
    assert principal.permissions == frozenset({APPROVE_REPAIR_PERMISSION})


def test_authenticator_fails_closed_for_missing_or_invalid_bearer_credentials() -> None:
    authenticator = ApprovalAuthenticator.from_environment(
        _identities(permissions=[APPROVE_REPAIR_PERMISSION])
    )

    with pytest.raises(ApprovalAuthenticationError, match="missing"):
        authenticator.require_approval(None)
    with pytest.raises(ApprovalAuthenticationError, match="invalid"):
        authenticator.require_approval("Bearer wrong-token")


def test_authenticator_rejects_identities_without_approval_permission() -> None:
    authenticator = ApprovalAuthenticator.from_environment(_identities(permissions=[]))

    with pytest.raises(ApprovalAuthorizationError, match="lacks"):
        authenticator.require_approval("Bearer configured-test-token")


def test_authenticator_requires_configuration_and_valid_environment_json() -> None:
    with pytest.raises(ApprovalAuthenticationConfigurationError, match="not configured"):
        ApprovalAuthenticator.from_environment({}).require_approval("Bearer token")
    with pytest.raises(ApprovalAuthenticationConfigurationError, match="valid identity"):
        ApprovalAuthenticator.from_environment({"INCIDENT_APPROVAL_IDENTITIES": "{}"})
