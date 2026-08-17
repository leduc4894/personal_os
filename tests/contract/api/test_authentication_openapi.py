"""Authentication OpenAPI contract: operations, schemes and snapshot freshness.

These tests pin the published authentication surface of the committed snapshot
against the brief's exact demands: every device-credential route keeps its
manually assigned semantic operation id, and the document carries the three
dedicated Bearer schemes of spec 16 — ``PollingCredential`` for the grant poll,
``RefreshCredential`` for rotation and self-revoke, and the declared
``AccessCredential`` of the access-authenticated device surface the later sync
children bind — each rendered exactly once as a distinct ``http``/``bearer``
scheme, and never as a URL path or query parameter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from api_runtime.openapi_export import render_openapi_json

SNAPSHOT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "packages" / "api-client" / "openapi.json"
)

#: The dedicated device Bearer schemes of spec 16, each named for exactly the
#: credential kind it authenticates.
DEVICE_BEARER_SCHEMES: Final[frozenset[str]] = frozenset(
    {"PollingCredential", "AccessCredential", "RefreshCredential"}
)

#: Route-to-scheme bindings of the credential-authenticated device and
#: plugin policy routes.
CREDENTIAL_ROUTE_SECURITY: Final[dict[str, str]] = {
    "/api/auth/device-authorizations/{grant_id}/poll": "PollingCredential",
    "/api/auth/device-tokens/refresh": "RefreshCredential",
    "/api/auth/device-tokens/revoke-current": "RefreshCredential",
    "/api/sync/exclusion-policy/keysets": "AccessCredential",
    "/api/sync/exclusion-policy/snapshot": "AccessCredential",
}


@pytest.fixture
def schema() -> dict[str, Any]:
    """Load the committed snapshot the generated client is built from."""
    return json.loads(SNAPSHOT_PATH.read_bytes())


def test_auth_openapi_has_semantic_operations_and_distinct_bearer_schemes(
    schema: dict[str, object],
) -> None:
    assert schema["paths"]["/api/auth/device-tokens/refresh"]["post"]["operationId"] == (
        "refreshDeviceToken"
    )
    assert {"PollingCredential", "AccessCredential", "RefreshCredential"} <= set(
        schema["components"]["securitySchemes"]
    )


def test_every_device_bearer_scheme_is_a_distinct_http_bearer_scheme(
    schema: dict[str, Any],
) -> None:
    security_schemes = schema["components"]["securitySchemes"]
    rendered_names = {name for name in security_schemes if name in DEVICE_BEARER_SCHEMES}
    assert rendered_names == DEVICE_BEARER_SCHEMES
    descriptions: set[str] = set()
    for scheme_name in DEVICE_BEARER_SCHEMES:
        scheme = security_schemes[scheme_name]
        assert scheme["type"] == "http", scheme_name
        assert scheme["scheme"] == "bearer", scheme_name
        assert isinstance(scheme.get("description"), str) and scheme["description"], scheme_name
        descriptions.add(scheme["description"])
    assert len(descriptions) == len(DEVICE_BEARER_SCHEMES)


def test_credential_routes_bind_exactly_their_dedicated_scheme(schema: dict[str, Any]) -> None:
    for path, scheme_name in CREDENTIAL_ROUTE_SECURITY.items():
        operation = schema["paths"][path]["post" if "exclusion-policy" not in path else "get"]
        assert operation["security"] == [{scheme_name: []}], path


def test_cookie_authenticated_routes_advertise_no_bearer_scheme(schema: dict[str, Any]) -> None:
    # Browser/session routes authenticate through the cookie contract, never a
    # Bearer credential; the plan document advertises no security requirement.
    for path, path_item in schema["paths"].items():
        if path in CREDENTIAL_ROUTE_SECURITY:
            continue
        for method, operation in path_item.items():
            assert isinstance(operation, dict)
            assert not operation.get("security"), (path, method)


def test_snapshot_is_the_deterministic_fresh_render(schema: dict[str, Any]) -> None:
    assert SNAPSHOT_PATH.read_bytes() == render_openapi_json()
    assert json.loads(render_openapi_json()) == schema
