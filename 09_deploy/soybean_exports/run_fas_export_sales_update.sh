#!/usr/bin/env bash

set -euo pipefail

PATH="${MARKET_DATA_WRAPPER_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
export PATH

SECRET_FILE="${MARKET_DATA_FAS_ENV_FILE:-/home/ubuntu/.config/market-data/fas-export-sales.env}"
LOCK_FILE="${MARKET_DATA_FAS_LOCK_FILE:-/run/lock/fas_export_sales_update.lock}"
LOG_ROOT="${MARKET_DATA_FAS_LOG_ROOT:-/home/ubuntu/market-data-runtime/soybean-exports/logs}"
LOG_FILE="${MARKET_DATA_FAS_LOG_FILE:-${LOG_ROOT}/fas_export_sales_update.log}"
CONTAINER_NAME="${MARKET_DATA_SPREAD_CONTAINER:-spread-dashboard}"
HOST_RUNTIME_ROOT="${MARKET_DATA_FAS_RUNTIME_HOST_ROOT:-/home/ubuntu/market-data}"
CONTAINER_RUNTIME_ROOT="${MARKET_DATA_FAS_CONTAINER_RUNTIME_ROOT:-/runtime}"
CONTAINER_ENTRYPOINT="${MARKET_DATA_FAS_ENTRYPOINT:-/app/04_scripts/soybean_exports/run_fas_export_sales.py}"

cleanup() {
    unset FAS_EXPORT_SALES_API_KEY MARKET_DATA_GIT_HEAD CONTAINER_IMAGE_ID OCI_REVISION USDA_API_KEY
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

fail() {
    echo "FAS Export Sales update failed: $*" >&2
    exit 1
}

for required_command in chmod date docker flock mkdir touch; do
    command -v "${required_command}" >/dev/null 2>&1 || \
        fail "missing required command ${required_command}"
done

umask 077
mkdir -p "${LOG_ROOT}" || fail "cannot create FAS log directory"
chmod 0700 "${LOG_ROOT}" || fail "cannot protect FAS log directory"
touch "${LOG_FILE}" || fail "cannot create FAS log file"
chmod 0600 "${LOG_FILE}" || fail "cannot protect FAS log file"
exec >>"${LOG_FILE}" 2>&1
echo "FAS Export Sales wrapper started at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

exec 9>"${LOCK_FILE}" || fail "cannot open FAS update lock"
if ! flock -n 9; then
    echo "FAS Export Sales update already has a running instance; this run was skipped."
    exit 0
fi

[[ -f "${SECRET_FILE}" && -r "${SECRET_FILE}" ]] || \
    fail "FAS secret file is absent or unreadable"
[[ -d "${HOST_RUNTIME_ROOT}/01_data" ]] || fail "runtime 01_data directory does not exist"

FAS_EXPORT_SALES_API_KEY=""
fas_key_lines=0
while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    case "${line}" in
        FAS_EXPORT_SALES_API_KEY=*)
            fas_key_lines=$((fas_key_lines + 1))
            FAS_EXPORT_SALES_API_KEY="${line#FAS_EXPORT_SALES_API_KEY=}"
            ;;
    esac
done <"${SECRET_FILE}"

if [[ "${fas_key_lines}" -ne 1 || -z "${FAS_EXPORT_SALES_API_KEY}" ]]; then
    fail "FAS_EXPORT_SALES_API_KEY is missing, empty, or duplicated"
fi

# Production must never inherit or fall back to the local development credential name.
unset USDA_API_KEY

if ! container_running="$(
    docker inspect --format '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null
)"; then
    fail "configured spread container does not exist"
fi
[[ "${container_running}" == "true" ]] || fail "configured spread container is not running"

if ! CONTAINER_IMAGE_ID="$(
    docker inspect --format '{{.Image}}' "${CONTAINER_NAME}" 2>/dev/null
)"; then
    fail "cannot resolve the configured spread image identity"
fi
[[ "${CONTAINER_IMAGE_ID}" =~ ^sha256:[0-9a-f]{64}$ ]] || \
    fail "configured spread image identity is invalid"

if ! MARKET_DATA_GIT_HEAD="$(
    docker exec "${CONTAINER_NAME}" printenv MARKET_DATA_GIT_HEAD 2>/dev/null
)"; then
    fail "cannot read runtime Git identity"
fi
[[ "${MARKET_DATA_GIT_HEAD}" =~ ^[0-9a-fA-F]{40}$ ]] || \
    fail "runtime Git identity is missing or invalid"
MARKET_DATA_GIT_HEAD="${MARKET_DATA_GIT_HEAD,,}"

if ! OCI_REVISION="$(
    docker image inspect \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "${CONTAINER_IMAGE_ID}" 2>/dev/null
)"; then
    fail "cannot read formal image OCI revision"
fi
[[ "${OCI_REVISION}" =~ ^[0-9a-fA-F]{40}$ ]] || fail "formal image OCI revision is invalid"
OCI_REVISION="${OCI_REVISION,,}"
[[ "${OCI_REVISION}" == "${MARKET_DATA_GIT_HEAD}" ]] || \
    fail "formal image OCI revision and runtime Git identity disagree"

echo "runtime_git_head=${MARKET_DATA_GIT_HEAD}"
echo "oci_revision=${OCI_REVISION}"
echo "formal_image_id=${CONTAINER_IMAGE_ID}"

export FAS_EXPORT_SALES_API_KEY MARKET_DATA_GIT_HEAD

pipeline_exit=0
docker run --rm \
    --env FAS_EXPORT_SALES_API_KEY \
    --env MARKET_DATA_GIT_HEAD \
    --mount "type=bind,src=${HOST_RUNTIME_ROOT}/01_data,dst=${CONTAINER_RUNTIME_ROOT}/01_data" \
    --entrypoint python \
    "${CONTAINER_IMAGE_ID}" \
    "${CONTAINER_ENTRYPOINT}" \
    --runtime-root "${CONTAINER_RUNTIME_ROOT}" \
    "$@" || pipeline_exit=$?

if (( pipeline_exit != 0 )); then
    echo "FAS container pipeline failed with exit code ${pipeline_exit}." >&2
    exit "${pipeline_exit}"
fi
echo "FAS container pipeline completed successfully."
exit 0
