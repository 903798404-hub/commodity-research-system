#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
    echo "usage: bash deploy_spread_release.sh <release-directory> <deployment-plan> [health-url]" >&2
    exit 64
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository="$(cd -- "${script_dir}/../.." && pwd)"
release_directory="$(cd -- "$1" && pwd)"
deployment_plan="$(cd -- "$(dirname -- "$2")" && pwd)/$(basename -- "$2")"
health_url="${3:-http://127.0.0.1:8501/_stcore/health}"
manifest="${release_directory}/release.json"
environment_file="${release_directory}/release.env"
verifier="${script_dir}/verify_release_contract.py"
rollback_script="${script_dir}/rollback_spread_release.sh"
readiness_waiter="${script_dir}/wait_for_service_ready.py"
readiness_result="${release_directory}/production_readiness.json"
readiness_failure="${release_directory}/production_readiness.failure.json"

python3 "${verifier}" \
    --phase pre-deploy \
    --repository "${repository}" \
    --manifest "${manifest}" \
    --env-file "${environment_file}" \
    --deployment-plan "${deployment_plan}"

readarray -t plan_identity < <(
    python3 - "${deployment_plan}" <<'PY'
import json
import sys

plan = json.load(open(sys.argv[1], encoding="utf-8"))
print(plan["release_id"])
print(plan["expected_image_id"])
print(plan["production_env_file"])
PY
)
if [[ "${#plan_identity[@]}" -ne 3 ]]; then
    echo "deployment plan identity could not be read" >&2
    exit 65
fi
RELEASE_ID="${plan_identity[0]}"
EXPECTED_IMAGE_ID="${plan_identity[1]}"
production_env_file="${plan_identity[2]}"

switch_started=0
deployment_succeeded=0

rollback_on_failure() {
    local exit_code=$?
    trap - ERR
    if [[ "${switch_started}" -eq 1 && "${deployment_succeeded}" -eq 0 ]]; then
        echo "deployment failed; restoring the manifest-sealed rollback Image ID" >&2
        if ! bash "${rollback_script}" "${release_directory}" "${deployment_plan}" "${health_url}"; then
            echo "automatic rollback also failed; manual intervention is required" >&2
        fi
    fi
    exit "${exit_code}"
}
trap rollback_on_failure ERR

switch_started=1
docker compose \
    --env-file "${production_env_file}" \
    --project-name market-data \
    --project-directory "${repository}" \
    -f "${repository}/docker-compose.yml" \
    up -d --no-build --no-deps spread-dashboard

initial_restart_count="$(
    docker inspect --format '{{.RestartCount}}' spread-dashboard
)"

python3 "${verifier}" \
    --phase post-deploy \
    --repository "${repository}" \
    --manifest "${manifest}" \
    --env-file "${environment_file}" \
    --deployment-plan "${deployment_plan}"

python3 "${readiness_waiter}" \
    --container spread-dashboard \
    --health-url "${health_url}" \
    --expected-image-id "${EXPECTED_IMAGE_ID}" \
    --initial-restart-count "${initial_restart_count}" \
    --policy-file "${deployment_plan}" \
    --result "${readiness_result}" \
    --failure-artifact "${readiness_failure}"

python3 "${verifier}" \
    --phase record-deployment \
    --repository "${repository}" \
    --manifest "${manifest}" \
    --env-file "${environment_file}" \
    --deployment-plan "${deployment_plan}" \
    --readiness-result "${readiness_result}"

deployment_succeeded=1
trap - ERR
echo "spread release ${RELEASE_ID} deployed with Image ID ${EXPECTED_IMAGE_ID}"
