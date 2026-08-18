"""Closed exclusion-policy route set: exact membership, methods and operation ids.

These tests compose the real application over both offline runtimes and pin
the served policy surface of spec 16: the seven routes with exactly their
methods and their manually assigned semantic operation ids, no trailing-slash
alias, no other verb on any policy path, no workspace/device selector on any
request schema, and the dedicated ``AccessCredential`` Bearer scheme bound
only by the two plugin routes — the Admin policy routes authenticate through
the Web session contract and advertise no security requirement.
"""

from __future__ import annotations

from typing import Any, Final

import httpx
import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import compose_offline_web_authentication
from api_runtime.exclusion_policy_composition import compose_offline_exclusion_policy
from fastapi import FastAPI

from personal_os.runtime_configuration.models import RuntimeEnvironment

#: The closed policy route set with exactly the method each path serves.
POLICY_ROUTE_METHODS: Final[dict[str, frozenset[str]]] = {
    "/api/admin/exclusion-policy": frozenset({"GET"}),
    "/api/admin/exclusion-policy/draft": frozenset({"PUT"}),
    "/api/admin/exclusion-policy/previews": frozenset({"POST"}),
    "/api/admin/exclusion-policy/previews/{policy_preview_id}": frozenset({"GET"}),
    "/api/admin/exclusion-policy/publications": frozenset({"POST"}),
    "/api/sync/exclusion-policy/keysets": frozenset({"GET"}),
    "/api/sync/exclusion-policy/snapshot": frozenset({"GET"}),
}

#: The exact semantic operation ids of the seven policy operations.
POLICY_OPERATION_IDS: Final[dict[tuple[str, str], str]] = {
    ("/api/admin/exclusion-policy", "get"): "getExclusionPolicyStatus",
    ("/api/admin/exclusion-policy/draft", "put"): "replaceExclusionPolicyDraft",
    ("/api/admin/exclusion-policy/previews", "post"): "createExclusionPolicyPreview",
    (
        "/api/admin/exclusion-policy/previews/{policy_preview_id}",
        "get",
    ): "getExclusionPolicyPreview",
    ("/api/admin/exclusion-policy/publications", "post"): "publishExclusionPolicy",
    ("/api/sync/exclusion-policy/keysets", "get"): "listExclusionPolicyKeysets",
    ("/api/sync/exclusion-policy/snapshot", "get"): "getExclusionPolicySnapshot",
}


class _ReadyProbe:
    """Readiness probe stub: route-set inspection never consults it."""

    async def check(self) -> None: ...


@pytest.fixture
def application() -> FastAPI:
    return create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(),
        exclusion_policy=compose_offline_exclusion_policy(),
    )


def test_policy_routes_serve_exactly_their_one_method(application: FastAPI) -> None:
    served: dict[str, frozenset[str]] = {
        getattr(route, "path", ""): frozenset(getattr(route, "methods", set()))
        for route in application.routes
        if getattr(route, "path", "").startswith(
            ("/api/admin/exclusion-policy", "/api/sync/exclusion-policy")
        )
    }
    assert served == POLICY_ROUTE_METHODS


def test_policy_routes_carry_their_semantic_operation_ids(application: FastAPI) -> None:
    rendered: dict[tuple[str, str], str] = {}
    for route in application.routes:
        path = getattr(route, "path", "")
        if path not in POLICY_ROUTE_METHODS:
            continue
        assert route.operation_id is not None, path
        for method in POLICY_ROUTE_METHODS[path]:
            rendered[(path, method.lower())] = str(route.operation_id)
    assert rendered == POLICY_OPERATION_IDS


def test_no_policy_route_registers_a_trailing_slash_alias(application: FastAPI) -> None:
    for route in application.routes:
        path = getattr(route, "path", None)
        assert isinstance(path, str), route
        assert not path.endswith("/"), path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/exclusion-policy/",
        "/api/admin/exclusion-policy/draft/",
        "/api/admin/exclusion-policy/previews/",
        "/api/admin/exclusion-policy/publications/",
        "/api/sync/exclusion-policy/keysets/",
        "/api/sync/exclusion-policy/snapshot/",
    ],
)
async def test_policy_trailing_slash_spelling_is_not_an_alias(
    application: FastAPI, path: str
) -> None:
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api_route_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/admin/exclusion-policy"),
        ("GET", "/api/admin/exclusion-policy/draft"),
        ("PUT", "/api/admin/exclusion-policy/previews"),
        ("DELETE", "/api/admin/exclusion-policy/publications"),
        ("POST", "/api/sync/exclusion-policy/keysets"),
        ("PUT", "/api/sync/exclusion-policy/snapshot"),
    ],
)
async def test_wrong_method_on_a_policy_route_is_the_closed_envelope(
    application: FastAPI, method: str, path: str
) -> None:
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(method, path)
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "api_method_not_allowed"


def _request_schema(document: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    return dict(
        document["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]
    )


def test_request_schemas_are_closed_and_carry_no_workspace_selector(
    application: FastAPI,
) -> None:
    document = application.openapi()
    assert (
        _request_schema(document, "/api/admin/exclusion-policy/draft", "put").get("$ref")
        == "#/components/schemas/PolicyDraftReplaceRequest"
    )
    assert (
        _request_schema(document, "/api/admin/exclusion-policy/publications", "post").get("$ref")
        == "#/components/schemas/PolicyPublicationRequest"
    )
    schemas = document["components"]["schemas"]
    draft_schema = dict(schemas["PolicyDraftReplaceRequest"])
    publication_schema = dict(schemas["PolicyPublicationRequest"])
    for schema in (draft_schema, publication_schema):
        assert schema.get("additionalProperties") is False
        properties = set(schema["properties"])
        assert not properties & {
            "workspace_id",
            "device_id",
            "user_id",
            "actor_user_id",
            "signature",
            "signature_bytes",
            "revision_number",
        }, properties
    assert set(publication_schema["required"]) >= {"policy_preview_id", "confirmation"}
    rule_schema = dict(schemas["PolicyDraftRuleRequest"])
    assert rule_schema.get("additionalProperties") is False
    assert "workspace_id" not in rule_schema["properties"]


def test_plugin_routes_bind_exactly_the_access_bearer_scheme(
    application: FastAPI,
) -> None:
    document = application.openapi()
    for path in ("/api/sync/exclusion-policy/keysets", "/api/sync/exclusion-policy/snapshot"):
        operation = document["paths"][path]["get"]
        assert operation["security"] == [{"AccessCredential": []}], path
    for path in (
        "/api/admin/exclusion-policy",
        "/api/admin/exclusion-policy/draft",
        "/api/admin/exclusion-policy/previews",
        "/api/admin/exclusion-policy/previews/{policy_preview_id}",
        "/api/admin/exclusion-policy/publications",
    ):
        for operation in document["paths"][path].values():
            assert isinstance(operation, dict)
            assert not operation.get("security"), path


def test_publication_documents_the_required_idempotency_header(
    application: FastAPI,
) -> None:
    document = application.openapi()
    operation = document["paths"]["/api/admin/exclusion-policy/publications"]["post"]
    parameters = [parameter["name"] for parameter in operation.get("parameters", [])]
    assert "X-Idempotency-Key" in parameters
    idempotency = next(
        parameter
        for parameter in operation["parameters"]
        if parameter["name"] == "X-Idempotency-Key"
    )
    assert idempotency["required"] is True
    assert idempotency["in"] == "header"
