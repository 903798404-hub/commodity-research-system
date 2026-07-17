#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: bash deploy_spread_release.sh <release-directory> [health-url]" >&2
    exit 64
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository="$(cd -- "${script_dir}/../.." && pwd)"
release_directory="$(cd -- "$1" && pwd)"
health_url="${2:-http://127.0.0.1:8501/_stcore/health}"
manifest="${release_directory}/release.json"
environment_file="${release_directory}/release.env"
verifier="${script_dir}/verify_release_contract.py"
rollback_script="${script_dir}/rollback_spread_release.sh"

python3 "${verifier}" \
    --phase pre-deploy \
    --repository "${repository}" \
    --manifest "${manifest}" \
    --env-file "${environment_file}"

while IFS='=' read -r key value; do
    case "${key}" in
        RELEASE_ID|SPREAD_IMAGE|EXPECTED_IMAGE_ID|EXPECTED_GIT_COMMIT)
            printf -v "${key}" '%s' "${value}"
            ;;
        "")
            ;;
        *)
            echo "unexpected release.env key: ${key}" >&2
            exit 65
            ;;
    esac
done < "${environment_file}"
export RELEASE_ID SPREAD_IMAGE EXPECTED_IMAGE_ID EXPECTED_GIT_COMMIT
: "${RELEASE_ID:?release.env did not provide RELEASE_ID}"
: "${SPREAD_IMAGE:?release.env did not provide SPREAD_IMAGE}"
: "${EXPECTED_IMAGE_ID:?release.env did not provide EXPECTED_IMAGE_ID}"
: "${EXPECTED_GIT_COMMIT:?release.env did not provide EXPECTED_GIT_COMMIT}"

switch_started=0
deployment_succeeded=0

rollback_on_failure() {
    local exit_code=$?
    trap - ERR
    if [[ "${switch_started}" -eq 1 && "${deployment_succeeded}" -eq 0 ]]; then
        echo "deployment failed; restoring the manifest-sealed rollback Image ID" >&2
        if ! bash "${rollback_script}" "${release_directory}" "${health_url}"; then
            echo "automatic rollback also failed; manual intervention is required" >&2
        fi
    fi
    exit "${exit_code}"
}
trap rollback_on_failure ERR

switch_started=1
SPREAD_IMAGE="${SPREAD_IMAGE}" docker compose \
    --project-name market-data \
    --project-directory "${repository}" \
    -f "${repository}/docker-compose.yml" \
    up -d --no-build --no-deps spread-dashboard

python3 "${verifier}" \
    --phase post-deploy \
    --repository "${repository}" \
    --manifest "${manifest}" \
    --env-file "${environment_file}"

curl --fail --silent --show-error --max-time 30 "${health_url}" >/dev/null

python3 "${verifier}" \
    --phase record-deployment \
    --repository "${repository}" \
    --manifest "${manifest}" \
    --env-file "${environment_file}" \
    --http-status 200

deployment_succeeded=1
trap - ERR
echo "spread release ${RELEASE_ID} deployed with Image ID ${EXPECTED_IMAGE_ID}"
