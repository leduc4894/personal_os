#!/bin/sh
set -eu

password_file=/run/secrets/postgres_temporal_password
if [ ! -r "$password_file" ]; then
    printf '%s\n' temporal_secret_unavailable >&2
    exit 1
fi

temporal_password=$(cat "$password_file")
if [ -z "$temporal_password" ]; then
    printf '%s\n' temporal_secret_unavailable >&2
    exit 1
fi

bind_on_ip=$(getent hosts "$(hostname)" | awk 'NR == 1 { print $1 }')
if [ -z "$bind_on_ip" ]; then
    printf '%s\n' temporal_network_unavailable >&2
    exit 1
fi

POSTGRES_PWD=$temporal_password
BIND_ON_IP=$bind_on_ip
TEMPORAL_BROADCAST_ADDRESS=$bind_on_ip
export POSTGRES_PWD BIND_ON_IP TEMPORAL_BROADCAST_ADDRESS
unset temporal_password bind_on_ip password_file

# The Compose file secret keeps host ownership (0600, root-readable only on a
# real Linux host), so the container starts as root to read it and must drop
# back to the image user before handing control to the server. Busybox su
# preserves the exported environment for the child process.
if [ "$(id -u)" = "0" ]; then
    exec su temporal -c 'exec temporal-server --allow-no-auth start'
fi
exec temporal-server --allow-no-auth start
