"""Fail-closed authentication and authorization for approval operations."""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

APPROVE_REPAIR_PERMISSION = "repair:approve"
_IDENTITIES_ENVIRONMENT_VARIABLE = "INCIDENT_APPROVAL_IDENTITIES"


class ApprovalAuthenticationError(PermissionError):
    """Raised when an approval request does not present a valid identity."""


class ApprovalAuthorizationError(PermissionError):
    """Raised when an authenticated identity cannot approve repairs."""


class ApprovalAuthenticationConfigurationError(RuntimeError):
    """Raised when approval authentication has no usable deployment config."""


class ApprovalPrincipal(BaseModel):
    """Authenticated actor retained with the approval audit record."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    subject: str = Field(min_length=1)
    permissions: frozenset[str] = Field(default_factory=frozenset)
    identity_source: str = Field(min_length=1)


class _ConfiguredApprovalIdentity(ApprovalPrincipal):
    """A local-development bearer credential loaded only from process config."""

    token: str = Field(min_length=16, repr=False)


class ApprovalAuthenticator:
    """Authenticate configured Bearer credentials and enforce approval scope.

    Deployments may replace this adapter with an OIDC-backed instance through
    ``create_incident_router``. The built-in adapter intentionally starts with
    no identities, so an unconfigured environment cannot approve a repair.
    """

    def __init__(self, identities: Iterable[_ConfiguredApprovalIdentity] = ()) -> None:
        self._identities = tuple(identities)
        subjects = [identity.subject for identity in self._identities]
        if len(subjects) != len(set(subjects)):
            raise ValueError("approval identity subjects must be unique")

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> ApprovalAuthenticator:
        values = environment if environment is not None else os.environ
        raw_identities = values.get(_IDENTITIES_ENVIRONMENT_VARIABLE)
        if raw_identities is None:
            return cls()
        try:
            payload = json.loads(raw_identities)
            if not isinstance(payload, list):
                raise TypeError("must be a JSON array")
            identities = tuple(
                _ConfiguredApprovalIdentity.model_validate(item) for item in payload
            )
        except (json.JSONDecodeError, TypeError, ValidationError) as exc:
            raise ApprovalAuthenticationConfigurationError(
                f"{_IDENTITIES_ENVIRONMENT_VARIABLE} must be a valid identity array"
            ) from exc
        return cls(identities)

    def require_approval(self, authorization: str | None) -> ApprovalPrincipal:
        """Authenticate a Bearer token and require the repair-approval scope."""

        if not self._identities:
            raise ApprovalAuthenticationConfigurationError(
                "approval authentication is not configured"
            )
        token = _bearer_token(authorization)
        principal = next(
            (
                identity
                for identity in self._identities
                if hmac.compare_digest(token, identity.token)
            ),
            None,
        )
        if principal is None:
            raise ApprovalAuthenticationError("invalid approval bearer token")
        if APPROVE_REPAIR_PERMISSION not in principal.permissions:
            raise ApprovalAuthorizationError(
                "authenticated identity lacks repair approval permission"
            )
        return ApprovalPrincipal(
            subject=principal.subject,
            permissions=principal.permissions,
            identity_source=principal.identity_source,
        )


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise ApprovalAuthenticationError("missing Authorization header")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        raise ApprovalAuthenticationError("Authorization header must use Bearer authentication")
    return token.strip()


__all__ = [
    "APPROVE_REPAIR_PERMISSION",
    "ApprovalAuthenticationConfigurationError",
    "ApprovalAuthenticationError",
    "ApprovalAuthenticator",
    "ApprovalAuthorizationError",
    "ApprovalPrincipal",
]
