#!/bin/sh
set -eu

# The Compose file secret keeps host ownership (0600, owned by the host user),
# and the image entrypoint hand-rolls its readability check from mode bits and
# the effective user id, so even root cannot pass it for that file on a real
# Linux host (Docker Desktop on Windows masks this by presenting mounts as
# world-readable). Materialize a neo4j-owned copy; the entrypoint then reads
# it and performs its usual root-to-neo4j drop before the server starts.
if [ "$(id -u)" = "0" ]; then
    install -d -o neo4j -g neo4j -m 700 /run/neo4j-secrets
    install -o neo4j -g neo4j -m 400 /run/secrets/neo4j_auth /run/neo4j-secrets/neo4j_auth
    export NEO4J_AUTH_FILE=/run/neo4j-secrets/neo4j_auth
fi

exec /startup/docker-entrypoint.sh "$@"
