#!/bin/sh
set -eu

password_file=/run/secrets/postgres_temporal_password
if [ ! -r "$password_file" ]; then
    printf '%s\n' schema_unavailable >&2
    exit 1
fi

sql_password=$(cat "$password_file")
if [ -z "$sql_password" ]; then
    printf '%s\n' schema_unavailable >&2
    exit 1
fi

SQL_PASSWORD=$sql_password
export SQL_PASSWORD
unset sql_password

install -m 0755 /usr/local/bin/temporal /opt/knowledge/health/temporal

tool_output=/tmp/temporal-schema-tool.$$.log
trap 'rm -f "$tool_output"' EXIT HUP INT TERM

run_schema_tool() {
    database_name=$1
    shift
    timeout 30s temporal-sql-tool \
        --plugin postgres12 \
        --endpoint postgresql \
        --port 5432 \
        --user temporal_service \
        --database "$database_name" \
        "$@" >"$tool_output" 2>&1
}

read_current_version() {
    current_version=$(sed -n 's/.*current version \([0-9][0-9.]*\).*/\1/p' \
        "$tool_output")
    if ! printf '%s\n' "$current_version" | grep -Eq '^[0-9]+\.[0-9]+$'; then
        printf '%s\n' schema_version_invalid >&2
        exit 1
    fi
}

assert_version_not_ahead() {
    current_version=$1
    target_version=$2
    if ! printf '%s\n%s\n' "$current_version" "$target_version" \
        | sort -V -c 2>/dev/null; then
        printf '%s\n' schema_version_ahead >&2
        exit 1
    fi
}

classify_schema_failure() {
    if grep -Eiq 'ahead|greater than|higher than|newer than|downgrade' "$tool_output"; then
        printf '%s\n' schema_version_ahead >&2
    elif grep -Eiq 'invalid|malformed|parse|schema version|unsupported version' "$tool_output"; then
        printf '%s\n' schema_version_invalid >&2
    else
        printf '%s\n' schema_unavailable >&2
    fi
    exit 1
}

for schema_specification in \
    temporal:1.19:/etc/temporal/schema/postgresql/v12/temporal/versioned \
    temporal_visibility:1.14:/etc/temporal/schema/postgresql/v12/visibility/versioned
do
    database_name=${schema_specification%%:*}
    target_and_directory=${schema_specification#*:}
    target_version=${target_and_directory%%:*}
    schema_directory=${target_and_directory#*:}

    if run_schema_tool "$database_name" setup-schema -v 0.0; then
        :
    fi
    if ! run_schema_tool "$database_name" update-schema --schema-dir "$schema_directory"; then
        classify_schema_failure
    fi
    if ! run_schema_tool "$database_name" update-schema --schema-dir "$schema_directory"; then
        classify_schema_failure
    fi
    read_current_version
    assert_version_not_ahead "$current_version" "$target_version"
    if [ "$current_version" != "$target_version" ]; then
        printf '%s\n' schema_version_invalid >&2
        exit 1
    fi
done

unset SQL_PASSWORD current_version database_name schema_directory schema_specification
unset target_and_directory target_version
