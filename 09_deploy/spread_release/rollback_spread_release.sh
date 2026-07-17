#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
    echo "usage: bash rollback_spread_release.sh <release-directory> <deployment-plan> [health-url]" >&2
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

python3 "${verifier}" \
    --phase pre-rollback \
    --repository "${repository}" \
    --manifest "${manifest}" \
    --env-file "${environment_file}" \
    --deployment-plan "${deployment_plan}"

readarray -t rollback_identity < <(
    python3 - "${manifest}" "${deployment_plan}" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
plan = json.load(open(sys.argv[2], encoding="utf-8"))
print(manifest["rollback_image_ref"])
print(manifest["rollback_image_id"])
print(manifest["formal_git_commit"])
print(plan["production_env_file"])
PY
)
if [[ "${#rollback_identity[@]}" -ne 4 ]]; then
    echo "rollback identity could not be read from the sealed manifest" >&2
    exit 65
fi
rollback_image_ref="${rollback_identity[0]}"
rollback_image_id="${rollback_identity[1]}"
rollback_git_commit="${rollback_identity[2]}"
production_env_file="${rollback_identity[3]}"

SPREAD_IMAGE="${rollback_image_ref}" \
MARKET_DATA_GIT_HEAD="${rollback_git_commit}" \
docker compose \
    --env-file "${production_env_file}" \
    --project-name market-data \
    --project-directory "${repository}" \
    -f "${repository}/docker-compose.yml" \
    up -d --no-build --no-deps spread-dashboard

python3 "${verifier}" \
    --phase post-rollback \
    --repository "${repository}" \
    --manifest "${manifest}" \
    --env-file "${environment_file}" \
    --deployment-plan "${deployment_plan}"

curl --fail --silent --show-error --max-time 30 "${health_url}" >/dev/null
echo "spread rollback restored Image ID ${rollback_image_id}"
