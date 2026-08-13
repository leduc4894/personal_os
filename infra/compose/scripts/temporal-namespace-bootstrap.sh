#!/bin/sh
set -eu

describe_output=/tmp/temporal-namespace-describe.$$.json
trap 'rm -f "$describe_output"' EXIT HUP INT TERM

temporal_cli() {
    temporal \
        --address temporal:7233 \
        --client-connect-timeout 5s \
        --command-timeout 10s \
        --color never \
        --disable-config-file \
        "$@"
}

if temporal_cli --output json operator namespace describe --namespace knowledge \
    >"$describe_output" 2>&1; then
    compact_description=$(tr -d '\r\n ' <"$describe_output")
    retention=$(printf '%s' "$compact_description" | sed -n \
        -e 's/.*"retention":"\([^"]*\)".*/\1/p' \
        -e 's/.*"workflowExecutionRetentionTtl":"\([^"]*\)".*/\1/p' \
        -e 's/.*"workflowExecutionRetentionPeriod":"\([^"]*\)".*/\1/p' \
        -e 's/.*"workflowExecutionRetentionPeriod":{"seconds":"\{0,1\}\([0-9][0-9]*\)"\{0,1\}.*/\1/p')
    case "$retention" in
        7d|168h|168h0m|168h0m0s|604800|604800s)
            exit 0
            ;;
        *)
            printf '%s\n' namespace_contract_mismatch >&2
            exit 1
            ;;
    esac
fi

if grep -Eiq 'not found|notfound|does not exist' "$describe_output"; then
    if temporal_cli operator namespace create --namespace knowledge --retention 7d \
        >/dev/null 2>&1; then
        exit 0
    fi
fi

printf '%s\n' namespace_unavailable >&2
exit 1
