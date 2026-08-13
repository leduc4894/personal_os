from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_LOCK_PATH = REPO_ROOT / "infra" / "compose" / "images.lock.yaml"
COMPOSE_PATH = REPO_ROOT / "infra" / "compose" / "compose.yaml"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
APPROVED_IMAGES = {
    "postgresql": (
        "postgres:18.4-bookworm",
        "sha256:882236b897e39051d2368c5ccc6cda944904723506b2dfc97f2a8f5bc9afa382",
    ),
    "qdrant": (
        "qdrant/qdrant:v1.18.2",
        "sha256:75eab8c4ba42096724fdcfde8b4de0b5713d529dde32f285a1f86fdcb2c9e50c",
    ),
    "neo4j": (
        "neo4j:5.26.28-community",
        "sha256:ff32db30b2baff97971e441b46bfd9c832c1b62c970398ef579244c06b21d357",
    ),
    "redis": (
        "redis:8.6.4",
        "sha256:a051e4f48a5d0ceda6554974f3ad0f5369f4479197f36829332c1325cecad2b7",
    ),
    "temporal-server": (
        "temporalio/server:1.31.2",
        "sha256:b5ecdb8282bededae2a10c36e8d862e27d0bc2d247fc73c5416025997ab4a1da",
    ),
    "temporal-admin-tools": (
        "temporalio/admin-tools:1.31.2",
        "sha256:dbc5fcd6ee8f0f4d808bf765af9a87dea9d8a283abfdcfbd2fc148496ba66107",
    ),
    "temporal-ui": (
        "temporalio/ui:2.53.0",
        "sha256:810eba47f77a89b0e64e2e751478ca585d037bbd90c0951a2974a92a6c5adeb9",
    ),
    "temporal-cli": (
        "temporalio/temporal:1.8.0",
        "sha256:2c344b4a39b4489fc6944db095f628f0c30659836faf11780a4dc435599e80e3",
    ),
}


def _read_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_compose_has_exact_topology() -> None:
    compose = _read_yaml(COMPOSE_PATH)

    assert compose["name"] == "knowledge-local"
    assert set(compose["services"]) == {
        "postgresql",
        "qdrant",
        "neo4j",
        "redis",
        "temporal",
        "temporal-ui",
        "temporal-cli",
        "postgres-provision",
        "temporal-schema-setup",
        "temporal-namespace-bootstrap",
    }
    assert compose["networks"] == {
        "service-backplane": {"driver": "bridge", "internal": True}
    }
    assert set(compose["volumes"]) == {
        "postgres-data",
        "qdrant-data",
        "neo4j-data",
        "redis-data",
    }
    assert all(
        service["networks"] == ["service-backplane"]
        for service in compose["services"].values()
    )


def test_every_publication_is_loopback_and_approved() -> None:
    allowed = {
        "POSTGRES_PORT": "127.0.0.1:${POSTGRES_PORT:-5432}:5432",
        "QDRANT_HTTP_PORT": "127.0.0.1:${QDRANT_HTTP_PORT:-6333}:6333",
        "QDRANT_GRPC_PORT": "127.0.0.1:${QDRANT_GRPC_PORT:-6334}:6334",
        "NEO4J_HTTP_PORT": "127.0.0.1:${NEO4J_HTTP_PORT:-7474}:7474",
        "NEO4J_BOLT_PORT": "127.0.0.1:${NEO4J_BOLT_PORT:-7687}:7687",
        "REDIS_PORT": "127.0.0.1:${REDIS_PORT:-6379}:6379",
        "TEMPORAL_GRPC_PORT": "127.0.0.1:${TEMPORAL_GRPC_PORT:-7233}:7233",
        "TEMPORAL_UI_PORT": "127.0.0.1:${TEMPORAL_UI_PORT:-8080}:8080",
    }
    publications = [
        publication
        for service in _read_yaml(COMPOSE_PATH)["services"].values()
        for publication in service.get("ports", [])
    ]

    assert set(publications) == set(allowed.values())
    assert len(publications) == len(allowed)
    assert all(publication.startswith("127.0.0.1:${") for publication in publications)


def test_lock_is_unique_immutable_and_amd64() -> None:
    lock = _read_yaml(IMAGE_LOCK_PATH)
    entries = lock["images"]

    assert lock["version"] == 1
    assert len(entries) == 8
    assert len({entry["component"] for entry in entries}) == len(entries)
    assert len({entry["tagged_reference"] for entry in entries}) == len(entries)
    assert all(DIGEST_PATTERN.fullmatch(entry["manifest_digest"]) for entry in entries)
    assert all(entry["supported_platforms"] == ["linux/amd64"] for entry in entries)
    assert all(entry["verified_at"] == "2026-08-13" for entry in entries)
    assert {
        entry["component"]: (entry["tagged_reference"], entry["manifest_digest"])
        for entry in entries
    } == APPROVED_IMAGES


def test_every_compose_image_is_locked_and_amd64() -> None:
    compose = _read_yaml(COMPOSE_PATH)
    entries = _read_yaml(IMAGE_LOCK_PATH)["images"]
    locked_references = {
        f'{entry["tagged_reference"]}@{entry["manifest_digest"]}' for entry in entries
    }
    compose_references = {service["image"] for service in compose["services"].values()}

    assert compose_references == locked_references
    assert all(
        service["platform"] == "linux/amd64" for service in compose["services"].values()
    )


def test_services_have_exact_storage_and_resource_contracts() -> None:
    services = _read_yaml(COMPOSE_PATH)["services"]
    expected_resources = {
        "postgresql": ("1536m", 1.5),
        "qdrant": ("3g", 2.0),
        "neo4j": ("2560m", 2.0),
        "redis": ("256m", 0.5),
        "temporal": ("1g", 1.5),
        "temporal-ui": ("256m", 0.5),
        "temporal-cli": ("128m", 0.25),
    }
    expected_mounts = {
        "postgresql": ["postgres-data:/var/lib/postgresql"],
        "qdrant": ["qdrant-data:/qdrant/storage"],
        "neo4j": ["neo4j-data:/data"],
        "redis": ["redis-data:/data"],
        "temporal": [],
        "temporal-ui": [],
        "temporal-cli": [],
    }

    for service_name, (memory, cpus) in expected_resources.items():
        service = services[service_name]
        assert service["mem_limit"] == memory
        assert service["cpus"] == cpus
        assert service["restart"] == "unless-stopped"
    for service_name, mount in expected_mounts.items():
        assert services[service_name].get("volumes", []) == mount


def test_initializers_are_bounded_one_shot_jobs_with_read_only_scripts() -> None:
    services = _read_yaml(COMPOSE_PATH)["services"]
    expected_scripts = {
        "postgres-provision": (
            "./scripts/postgres-provision.sh:/opt/knowledge/bin/postgres-provision.sh:ro"
        ),
        "temporal-schema-setup": (
            "./scripts/temporal-schema-setup.sh:"
            "/opt/knowledge/bin/temporal-schema-setup.sh:ro"
        ),
        "temporal-namespace-bootstrap": (
            "./scripts/temporal-namespace-bootstrap.sh:"
            "/opt/knowledge/bin/temporal-namespace-bootstrap.sh:ro"
        ),
    }

    for service_name, expected_script in expected_scripts.items():
        service = services[service_name]
        assert service["restart"] == "no"
        assert service["mem_limit"] == "512m"
        assert service["cpus"] == 0.5
        assert service["volumes"] == [expected_script]


def test_credentials_are_file_backed_and_not_environment_selected() -> None:
    compose = _read_yaml(COMPOSE_PATH)
    secret_files = {
        secret["file"] for secret in compose["secrets"].values()
    }

    assert secret_files == {
        "../../.local/stack-secrets/postgres_admin_password",
        "../../.local/stack-secrets/postgres_application_password",
        "../../.local/stack-secrets/postgres_temporal_password",
        "../../.local/stack-secrets/qdrant_api_key",
        "../../.local/stack-secrets/qdrant_config.yaml",
        "../../.local/stack-secrets/neo4j_auth",
        "../../.local/stack-secrets/redis_acl",
        "../../.local/stack-secrets/redis_application_password",
    }
    for service in compose["services"].values():
        for key in service.get("environment", {}):
            assert not key.endswith(("PASSWORD", "PASSWORD_FILE", "API_KEY", "AUTH")) or str(
                service["environment"][key]
            ).startswith("/run/secrets/")


def test_forbidden_surfaces_are_absent() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "container_name:",
        "privileged: true",
        "network_mode: host",
        "/var/run/docker.sock",
        "latest",
        "6335:",
        "NEO4J_PLUGINS",
        "auto-setup",
        "start-dev",
        "minio",
        "cloudflarestorage.com",
        "R2_",
        "profiles:",
    ):
        assert forbidden not in text
