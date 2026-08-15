#!/bin/sh
set -eu

# The Compose file secret keeps host ownership (0600, root-readable only on a
# real Linux host), while the image entrypoint drops to the redis user before
# redis-server reads the ACL file. Materialize a redis-owned copy first; the
# official entrypoint then performs its usual root-to-redis drop.
if [ "$(id -u)" = "0" ]; then
    install -d -o redis -g redis -m 700 /run/redis
    install -o redis -g redis -m 400 /run/secrets/redis_acl /run/redis/redis_acl
fi

exec docker-entrypoint.sh "$@"
