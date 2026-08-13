from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_LOCK_PATH = REPO_ROOT / "infra" / "compose" / "images.lock.yaml"
COMPOSE_PATH = REPO_ROOT / "infra" / "compose" / "compose.yaml"
SCRIPT_DIRECTORY = REPO_ROOT / "infra" / "compose" / "scripts"
LOCAL_STACK_DESIGN_PATH = (
    REPO_ROOT / "docs" / "superpowers" / "specs" / "local-service-stack-design.md"
)
LOCAL_STACK_PLAN_PATH = (
    REPO_ROOT / "docs" / "superpowers" / "plans" / "2026-08-13-local-service-stack.md"
)
DOCKER_CONTRACT_ENVIRONMENT = "KNOWLEDGE_LOCAL_DOCKER_CONTRACT"
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


def _require_docker_contract() -> str:
    if os.environ.get(DOCKER_CONTRACT_ENVIRONMENT) != "1":
        pytest.skip(f"set {DOCKER_CONTRACT_ENVIRONMENT}=1 to run Docker contracts")

    docker = shutil.which("docker")
    if docker is None:
        pytest.fail("Docker contracts were requested but the Docker CLI is unavailable")
    daemon = subprocess.run(
        [docker, "info", "--format", "{{.ServerVersion}}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    if daemon.returncode != 0:
        pytest.fail("Docker contracts were requested but the Docker daemon is unavailable")
    return docker


def _read_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_docker_contract_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DOCKER_CONTRACT_ENVIRONMENT, raising=False)

    def fail_on_docker_discovery(_command: str) -> str:
        pytest.fail("Docker discovery must not run without explicit opt-in")

    monkeypatch.setattr(shutil, "which", fail_on_docker_discovery)
    with pytest.raises(pytest.skip.Exception):
        _require_docker_contract()


def _run_checked(
    command: list[str], *, timeout_seconds: int = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _run_postgres_provision(
    docker: str,
    *,
    network_name: str,
    secrets_directory: Path,
) -> subprocess.CompletedProcess[str]:
    services = _read_yaml(COMPOSE_PATH)["services"]
    provision_service = services["postgres-provision"]
    script_path = SCRIPT_DIRECTORY / "postgres-provision.sh"
    return subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--network",
            network_name,
            "--mount",
            f"type=bind,source={secrets_directory},target=/run/secrets,readonly",
            "--mount",
            f"type=bind,source={script_path},target=/opt/knowledge/bin/postgres-provision.sh,readonly",
            "--entrypoint",
            "/bin/sh",
            provision_service["image"],
            "/opt/knowledge/bin/postgres-provision.sh",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _postgres_admin_query(
    docker: str,
    *,
    container_name: str,
    query: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            docker,
            "exec",
            "-i",
            container_name,
            "/bin/sh",
            "-ec",
            "PGPASSWORD=$(cat /run/secrets/postgres_admin_password); "
            "export PGPASSWORD; "
            "exec psql -XAtq -v ON_ERROR_STOP=1 --host 127.0.0.1 --port 5432 "
            "--username stack_admin --dbname postgres",
        ],
        input=query,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def provisioned_postgresql(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, str | Path]]:
    docker = _require_docker_contract()
    suffix = uuid.uuid4().hex[:12]
    container_name = f"knowledge-contract-postgres-{suffix}"
    network_name = f"knowledge-contract-network-{suffix}"
    secrets_directory = tmp_path_factory.mktemp("postgres-contract-secrets")
    passwords = {
        "postgres_admin_password": f"admin_{secrets.token_hex(24)}",
        "postgres_application_password": f"application_{secrets.token_hex(24)}",
        "postgres_temporal_password": f"temporal_{secrets.token_hex(24)}",
    }
    for secret_name, secret_value in passwords.items():
        (secrets_directory / secret_name).write_text(secret_value, encoding="utf-8")

    services = _read_yaml(COMPOSE_PATH)["services"]
    postgresql = services["postgresql"]
    _run_checked([docker, "network", "create", network_name])
    try:
        start_command = [
            docker,
            "run",
            "--detach",
            "--rm",
            "--name",
            container_name,
            "--network",
            network_name,
            "--network-alias",
            "postgresql",
            "--mount",
            f"type=bind,source={secrets_directory},target=/run/secrets,readonly",
        ]
        for environment_name, environment_value in postgresql["environment"].items():
            start_command.extend(["--env", f"{environment_name}={environment_value}"])
        start_command.extend(
            [
                postgresql["image"],
                *postgresql["command"],
                "-c",
                "shared_preload_libraries=passwordcheck",
            ]
        )
        _run_checked(start_command, timeout_seconds=90)

        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            readiness = subprocess.run(
                [docker, "exec", container_name, "pg_isready", "--host", "127.0.0.1"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            if readiness.returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")

        provision = _run_postgres_provision(
            docker,
            network_name=network_name,
            secrets_directory=secrets_directory,
        )
        assert provision.returncode == 0, provision.stderr
        yield {
            "docker": docker,
            "container_name": container_name,
            "network_name": network_name,
            "secrets_directory": secrets_directory,
            **passwords,
        }
    finally:
        subprocess.run(
            [docker, "rm", "--force", "--volumes", container_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(
            [docker, "network", "rm", network_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )


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
    assert compose["networks"] == {"service-backplane": {"driver": "bridge", "internal": True}}
    assert set(compose["volumes"]) == {
        "postgres-data",
        "qdrant-data",
        "neo4j-data",
        "redis-data",
        "temporal-health-tools",
    }
    assert all(
        service["networks"] == ["service-backplane"] for service in compose["services"].values()
    )


def test_canonical_docs_authorize_exact_five_volume_reset_contract() -> None:
    expected_volumes = {
        "postgres-data",
        "qdrant-data",
        "neo4j-data",
        "redis-data",
        "temporal-health-tools",
    }
    design = LOCAL_STACK_DESIGN_PATH.read_text(encoding="utf-8")
    volume_contract = design.split("## 10. Persistent volumes and restart behavior", 1)[1].split(
        "## 11.", 1
    )[0]
    reset_contract = design.split("## 15. Reset safety", 1)[1].split("## 16.", 1)[0]
    plan = LOCAL_STACK_PLAN_PATH.read_text(encoding="utf-8")
    reset_plan = plan.split("- [ ] **Step 4: Implement exact-label reset**", 1)[1].split(
        "- [ ] **Step 5:", 1
    )[0]

    assert {volume for volume in expected_volumes if f"`{volume}`" in volume_contract} == (
        expected_volumes
    )
    assert "rebuildable" in volume_contract.lower()
    assert {volume for volume in expected_volumes if volume in reset_contract} == expected_volumes
    assert "unknown labeled volumes" in reset_contract.lower()
    assert {volume for volume in expected_volumes if volume in reset_plan} == expected_volumes
    assert "Reject unknown labeled volumes" in reset_plan


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
        f"{entry['tagged_reference']}@{entry['manifest_digest']}" for entry in entries
    }
    compose_references = {service["image"] for service in compose["services"].values()}

    assert compose_references == locked_references
    assert all(service["platform"] == "linux/amd64" for service in compose["services"].values())


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
        "temporal": [
            "./config/temporal/dynamicconfig.yaml:/etc/temporal/dynamicconfig.yaml:ro",
            "./scripts/temporal-secret-entrypoint.sh:"
            "/opt/knowledge/bin/temporal-secret-entrypoint.sh:ro",
            "temporal-health-tools:/opt/knowledge/health:ro",
        ],
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
            "./scripts/temporal-schema-setup.sh:/opt/knowledge/bin/temporal-schema-setup.sh:ro",
            "temporal-health-tools:/opt/knowledge/health",
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
        expected_volumes = (
            list(expected_script) if isinstance(expected_script, tuple) else [expected_script]
        )
        assert service["volumes"] == expected_volumes


def test_credentials_are_file_backed_and_not_environment_selected() -> None:
    compose = _read_yaml(COMPOSE_PATH)
    secret_files = {secret["file"] for secret in compose["secrets"].values()}

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


def test_initializers_have_exact_dependency_chain() -> None:
    services = _read_yaml(COMPOSE_PATH)["services"]

    assert services["postgres-provision"]["depends_on"] == {
        "postgresql": {"condition": "service_healthy"}
    }
    assert services["temporal-schema-setup"]["depends_on"] == {
        "postgres-provision": {"condition": "service_completed_successfully"}
    }
    assert services["temporal"]["depends_on"] == {
        "temporal-schema-setup": {"condition": "service_completed_successfully"}
    }
    assert services["temporal-namespace-bootstrap"]["depends_on"] == {
        "temporal": {"condition": "service_healthy"}
    }
    assert services["temporal-ui"]["depends_on"] == {
        "temporal-namespace-bootstrap": {"condition": "service_completed_successfully"}
    }
    assert services["temporal-cli"]["depends_on"] == {
        "temporal-namespace-bootstrap": {"condition": "service_completed_successfully"}
    }


def test_initialization_scripts_use_strict_bounded_secret_safe_shell() -> None:
    script_names = {
        "postgres-provision.sh",
        "temporal-schema-setup.sh",
        "temporal-namespace-bootstrap.sh",
        "temporal-secret-entrypoint.sh",
    }

    for script_name in script_names:
        script = (SCRIPT_DIRECTORY / script_name).read_text(encoding="utf-8")
        assert script.startswith("#!/bin/sh\nset -eu\n")
        assert "set -x" not in script
        assert "printenv" not in script
        assert "while true" not in script
        assert "until " not in script
        assert not re.search(r"\b(?:CREATE TABLE|CREATE EXTENSION|alembic)\b", script, re.I)
        assert not re.search(r"(?:echo|printf).*\$\{?\w*(?:PASSWORD|SECRET)", script, re.I)


def test_postgres_18_provisioning_reconciles_exact_roles_databases_and_grants() -> None:
    compose = _read_yaml(COMPOSE_PATH)
    postgresql = compose["services"]["postgresql"]
    script = (SCRIPT_DIRECTORY / "postgres-provision.sh").read_text(encoding="utf-8")

    assert "postgres-data:/var/lib/postgresql" in postgresql["volumes"]
    assert postgresql["environment"] == {
        "POSTGRES_USER": "stack_admin",
        "POSTGRES_DB": "postgres",
        "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_admin_password",
        "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256 --auth-local=scram-sha-256",
    }
    assert postgresql["command"] == [
        "postgres",
        "-c",
        "shared_buffers=256MB",
        "-c",
        "password_encryption=scram-sha-256",
        "-c",
        "log_statement=none",
        "-c",
        "log_min_duration_statement=-1",
        "-c",
        "log_parameter_max_length=0",
        "-c",
        "log_min_error_statement=panic",
    ]
    for contract_fragment in (
        "psql -XAtq -v ON_ERROR_STOP=1",
        "format('%I'",
        "format('%L'",
        "knowledge_app",
        "temporal_service",
        "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION",
        "('knowledge', 'knowledge_app')",
        "('temporal', 'temporal_service')",
        "('temporal_visibility', 'temporal_service')",
        "REVOKE CONNECT, CREATE ON DATABASE",
        "REVOKE ALL PRIVILEGES ON DATABASE",
        "GRANT CONNECT ON DATABASE",
    ):
        assert contract_fragment in script


def test_temporal_schema_script_is_version_aware_and_fails_closed() -> None:
    script = (SCRIPT_DIRECTORY / "temporal-schema-setup.sh").read_text(encoding="utf-8")

    for contract_fragment in (
        "postgres12",
        "/etc/temporal/schema/postgresql/v12/temporal/versioned",
        "/etc/temporal/schema/postgresql/v12/visibility/versioned",
        "temporal:1.19",
        "temporal_visibility:1.14",
        "setup-schema -v 0.0",
        "update-schema --schema-dir",
        "schema_version_ahead",
        "schema_version_invalid",
        "schema_unavailable",
        "timeout 30s temporal-sql-tool",
        "sort -V -c 2>/dev/null",
    ):
        assert contract_fragment in script
    assert "update-schema --version" not in script


def test_namespace_is_exactly_knowledge_with_seven_day_retention() -> None:
    script = (SCRIPT_DIRECTORY / "temporal-namespace-bootstrap.sh").read_text(encoding="utf-8")

    assert "operator namespace describe" in script
    assert "operator namespace create" in script
    assert "--namespace knowledge" in script
    assert "--retention 7d" in script
    assert "604800" in script
    assert '"workflowExecutionRetentionTtl"' in script
    assert "namespace_contract_mismatch" in script
    assert "--client-connect-timeout 5s" in script
    assert "--command-timeout 10s" in script


def test_services_use_only_required_secrets_commands_and_static_config() -> None:
    compose = _read_yaml(COMPOSE_PATH)
    services = compose["services"]
    expected_secrets = {
        "postgresql": ["postgres_admin_password"],
        "qdrant": [{"source": "qdrant_config", "target": "qdrant_config.yaml"}],
        "neo4j": ["neo4j_auth"],
        "redis": ["redis_acl"],
        "temporal": ["postgres_temporal_password"],
        "temporal-ui": [],
        "temporal-cli": [],
        "postgres-provision": [
            "postgres_admin_password",
            "postgres_application_password",
            "postgres_temporal_password",
        ],
        "temporal-schema-setup": ["postgres_temporal_password"],
        "temporal-namespace-bootstrap": [],
    }

    assert {
        service_name: service.get("secrets", []) for service_name, service in services.items()
    } == expected_secrets
    assert services["qdrant"]["command"] == [
        "--config-path",
        "/run/secrets/qdrant_config.yaml",
        "--disable-telemetry",
    ]
    assert services["qdrant"]["entrypoint"] == ["/qdrant/entrypoint.sh"]
    assert services["redis"]["command"] == [
        "redis-server",
        "--aclfile",
        "/run/secrets/redis_acl",
        "--appendonly",
        "yes",
        "--maxmemory",
        "128mb",
        "--maxmemory-policy",
        "noeviction",
        "--protected-mode",
        "yes",
    ]
    assert services["temporal"]["entrypoint"] == [
        "/bin/sh",
        "/opt/knowledge/bin/temporal-secret-entrypoint.sh",
    ]
    assert services["temporal"]["environment"] == {
        "DB": "postgres12",
        "DB_PORT": "5432",
        "POSTGRES_USER": "temporal_service",
        "POSTGRES_SEEDS": "postgresql",
        "DBNAME": "temporal",
        "VISIBILITY_DBNAME": "temporal_visibility",
        "DYNAMIC_CONFIG_FILE_PATH": "/etc/temporal/dynamicconfig.yaml",
        "NUM_HISTORY_SHARDS": "4",
    }
    assert services["temporal-ui"]["environment"] == {"TEMPORAL_ADDRESS": "temporal:7233"}
    assert services["temporal-cli"]["entrypoint"] == ["/bin/sh", "-ec"]
    assert services["temporal-cli"]["command"] == ["exec sleep 2147483647"]

    neo4j_environment = services["neo4j"]["environment"]
    assert "NEO4J_PLUGINS" not in neo4j_environment
    assert neo4j_environment["NEO4J_server_memory_heap_initial__size"] == "512m"
    assert neo4j_environment["NEO4J_server_memory_heap_max__size"] == "1024m"
    assert neo4j_environment["NEO4J_server_memory_pagecache_size"] == "1024m"
    assert neo4j_environment["NEO4J_server_https_enabled"] == "false"
    assert neo4j_environment["NEO4J_dbms_usage__report_enabled"] == "false"

    dynamic_config = _read_yaml(
        REPO_ROOT / "infra" / "compose" / "config" / "temporal" / "dynamicconfig.yaml"
    )
    assert dynamic_config == {"limit.maxIDLength": [{"value": 255, "constraints": {}}]}


def test_long_running_services_have_exact_health_contracts() -> None:
    services = _read_yaml(COMPOSE_PATH)["services"]
    expected = {
        "postgresql": ("10s", "psql"),
        "qdrant": ("10s", "bash"),
        "neo4j": ("60s", "cypher-shell"),
        "redis": ("10s", "redis-cli"),
        "temporal": ("60s", "/opt/knowledge/health/temporal"),
        "temporal-ui": ("30s", "wget"),
        "temporal-cli": ("30s", "temporal"),
    }

    for service_name, (start_period, binary) in expected.items():
        healthcheck = services[service_name]["healthcheck"]
        assert healthcheck["interval"] == "5s"
        assert healthcheck["timeout"] == "3s"
        assert healthcheck["retries"] == 20
        assert healthcheck["start_period"] == start_period
        assert healthcheck["test"][0] == "CMD-SHELL"
        assert re.search(rf"\bexec {re.escape(binary)}\b", healthcheck["test"][1])
    assert "operator cluster health" in services["temporal"]["healthcheck"]["test"][1]
    assert '--address "$$(hostname):7233"' in services["temporal"]["healthcheck"]["test"][1]


def test_postgres_provision_reconciles_public_membership_and_bypassrls_drift(
    provisioned_postgresql: dict[str, str | Path],
) -> None:
    docker = str(provisioned_postgresql["docker"])
    container_name = str(provisioned_postgresql["container_name"])
    drift_query = """
ALTER ROLE knowledge_app BYPASSRLS;
GRANT temporal_service TO knowledge_app;
GRANT knowledge_app TO stack_admin;
GRANT CONNECT ON DATABASE postgres TO PUBLIC;
GRANT CONNECT ON DATABASE knowledge TO PUBLIC;
GRANT CONNECT ON DATABASE temporal TO PUBLIC;
GRANT CONNECT ON DATABASE temporal_visibility TO PUBLIC;
"""
    drift = _postgres_admin_query(
        docker,
        container_name=container_name,
        query=drift_query,
    )
    assert drift.returncode == 0, drift.stderr

    provision = _run_postgres_provision(
        docker,
        network_name=str(provisioned_postgresql["network_name"]),
        secrets_directory=Path(provisioned_postgresql["secrets_directory"]),
    )
    assert provision.returncode == 0, provision.stderr

    state = _postgres_admin_query(
        docker,
        container_name=container_name,
        query="""
SELECT rolname || ':' || rolbypassrls
FROM pg_roles
WHERE rolname IN ('knowledge_app', 'temporal_service')
ORDER BY rolname;
SELECT COUNT(*)
FROM pg_auth_members members
JOIN pg_roles member_role ON member_role.oid = members.member
JOIN pg_roles granted_role ON granted_role.oid = members.roleid
WHERE member_role.rolname IN ('knowledge_app', 'temporal_service')
   OR granted_role.rolname IN ('knowledge_app', 'temporal_service');
SELECT databases.datname || ':' || EXISTS (
    SELECT 1
    FROM aclexplode(
        COALESCE(databases.datacl, acldefault('d', databases.datdba))
    ) AS privileges
    WHERE privileges.grantee = 0
      AND privileges.privilege_type = 'CONNECT'
)
FROM pg_database AS databases
WHERE datname IN ('knowledge', 'postgres', 'temporal', 'temporal_visibility')
ORDER BY datname;
""",
    )
    assert state.returncode == 0, state.stderr
    assert state.stdout.splitlines() == [
        "knowledge_app:false",
        "temporal_service:false",
        "0",
        "knowledge:false",
        "postgres:false",
        "temporal:false",
        "temporal_visibility:false",
    ]


def test_postgres_provision_never_emits_secret_values_on_failure(
    provisioned_postgresql: dict[str, str | Path],
) -> None:
    docker = str(provisioned_postgresql["docker"])
    container_name = str(provisioned_postgresql["container_name"])
    application_password = "leaky"
    secrets_directory = Path(provisioned_postgresql["secrets_directory"])
    (secrets_directory / "postgres_application_password").write_text(
        application_password,
        encoding="utf-8",
    )

    provision = _run_postgres_provision(
        docker,
        network_name=str(provisioned_postgresql["network_name"]),
        secrets_directory=secrets_directory,
    )
    logs = subprocess.run(
        [docker, "logs", container_name],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined_output = "\n".join((provision.stdout, provision.stderr, logs.stdout, logs.stderr))

    assert provision.returncode != 0
    assert application_password not in combined_output
    for secret_name in (
        "postgres_admin_password",
        "postgres_application_password",
        "postgres_temporal_password",
    ):
        assert str(provisioned_postgresql[secret_name]) not in combined_output


def test_locked_health_binaries_include_a_working_temporal_grpc_bridge(tmp_path: Path) -> None:
    docker = _require_docker_contract()
    compose = _read_yaml(COMPOSE_PATH)
    services = compose["services"]
    long_running_services = {
        "postgresql",
        "qdrant",
        "neo4j",
        "redis",
        "temporal",
        "temporal-ui",
        "temporal-cli",
    }
    health_binaries: dict[str, str] = {}
    for service_name in long_running_services:
        test_command = services[service_name]["healthcheck"]["test"]
        matches = re.findall(r"\bexec ([A-Za-z0-9_./-]+)", test_command[1])
        assert matches
        health_binaries[service_name] = matches[0]

    locked_references = {
        f"{entry['tagged_reference']}@{entry['manifest_digest']}"
        for entry in _read_yaml(IMAGE_LOCK_PATH)["images"]
    }
    for image_reference in locked_references:
        pull = subprocess.run(
            [docker, "pull", image_reference],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
        )
        assert pull.returncode == 0

    for service_name, binary in health_binaries.items():
        probe_image = services[service_name]["image"]
        probe_binary = binary
        if service_name == "temporal":
            probe_image = services["temporal-schema-setup"]["image"]
            probe_binary = "/usr/local/bin/temporal"
        probe = subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--entrypoint",
                "/bin/sh",
                probe_image,
                "-ec",
                'command -v "$1" >/dev/null',
                "healthcheck",
                probe_binary,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        assert probe.returncode == 0, f"{service_name} lacks health binary {binary}"

    volume_name = f"knowledge-contract-temporal-health-{uuid.uuid4().hex[:12]}"
    network_name = f"knowledge-contract-temporal-health-{uuid.uuid4().hex[:12]}"
    server_name = f"knowledge-contract-temporal-health-{uuid.uuid4().hex[:12]}"
    fake_tools_directory = tmp_path / "contract-bin"
    fake_tools_directory.mkdir()
    (fake_tools_directory / "temporal-sql-tool").write_text(
        """#!/bin/sh
case " $* " in
    *" setup-schema "*) exit 1 ;;
    *" --database temporal_visibility "*) printf '%s\\n' 'current version 1.14' ;;
        *) printf '%s\\n' 'current version 1.19' ;;
esac
""",
        encoding="utf-8",
        newline="\n",
    )
    secrets_directory = tmp_path / "secrets"
    secrets_directory.mkdir()
    (secrets_directory / "postgres_temporal_password").write_text(
        f"temporal_{secrets.token_hex(24)}",
        encoding="utf-8",
    )
    _run_checked([docker, "volume", "create", volume_name])
    try:
        _run_checked([docker, "network", "create", network_name])
        install_probe = subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--user",
                services["temporal-schema-setup"]["user"],
                "--volume",
                f"{volume_name}:/opt/knowledge/health",
                "--mount",
                f"type=bind,source={secrets_directory},target=/run/secrets,readonly",
                "--mount",
                f"type=bind,source={fake_tools_directory},target=/contract-bin,readonly",
                "--mount",
                "type=bind,"
                f"source={SCRIPT_DIRECTORY / 'temporal-schema-setup.sh'},"
                "target=/opt/knowledge/bin/temporal-schema-setup.sh,readonly",
                "--env",
                "PATH=/contract-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "--entrypoint",
                "/bin/sh",
                services["temporal-schema-setup"]["image"],
                "/opt/knowledge/bin/temporal-schema-setup.sh",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        assert install_probe.returncode == 0
        _run_checked(
            [
                docker,
                "run",
                "--detach",
                "--rm",
                "--name",
                server_name,
                "--network",
                network_name,
                "--network-alias",
                "temporal-health-backend",
                "--entrypoint",
                "/usr/local/bin/temporal",
                services["temporal-cli"]["image"],
                "--log-level",
                "error",
                "server",
                "start-dev",
                "--headless",
                "--ip",
                "0.0.0.0",
            ],
            timeout_seconds=30,
        )
        deadline = time.monotonic() + 30
        while True:
            execution_probe = subprocess.run(
                [
                    docker,
                    "run",
                    "--rm",
                    "--network",
                    network_name,
                    "--volume",
                    f"{volume_name}:/opt/knowledge/health:ro",
                    "--entrypoint",
                    "/opt/knowledge/health/temporal",
                    services["temporal"]["image"],
                    "--address",
                    "temporal-health-backend:7233",
                    "--client-connect-timeout",
                    "2s",
                    "--command-timeout",
                    "2s",
                    "--color",
                    "never",
                    "--disable-config-file",
                    "operator",
                    "cluster",
                    "health",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            if execution_probe.returncode == 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        assert execution_probe.returncode == 0
    finally:
        subprocess.run(
            [docker, "rm", "--force", "--volumes", server_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(
            [docker, "network", "rm", network_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(
            [docker, "volume", "rm", "--force", volume_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
