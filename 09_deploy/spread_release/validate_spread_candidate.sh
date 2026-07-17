#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
    echo "usage: bash validate_spread_candidate.sh <release-directory> <health-url> <readiness-result> [failure-artifact]" >&2
    exit 64
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository="$(cd -- "${script_dir}/../.." && pwd)"
release_directory="$(cd -- "$1" && pwd)"
health_url="$2"
readiness_result="$(cd -- "$(dirname -- "$3")" && pwd)/$(basename -- "$3")"
failure_artifact="${4:-${readiness_result%.json}.failure.json}"
failure_artifact="$(cd -- "$(dirname -- "${failure_artifact}")" && pwd)/$(basename -- "${failure_artifact}")"
manifest="${release_directory}/release.json"
environment_file="${release_directory}/release.env"
verifier="${script_dir}/verify_release_contract.py"
readiness_waiter="${script_dir}/wait_for_service_ready.py"

readarray -t identity < <(
    python3 - "${manifest}" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
print(manifest["candidate_container_name"])
print(manifest["image_id"])
PY
)
if [[ "${#identity[@]}" -ne 2 ]]; then
    echo "candidate identity could not be read from release.json" >&2
    exit 65
fi
candidate_container="${identity[0]}"
expected_image_id="${identity[1]}"
initial_restart_count="$(
    docker inspect --format '{{.RestartCount}}' "${candidate_container}"
)"

# Immutable identity is always verified before the first readiness request.
python3 "${verifier}" \
    --phase candidate \
    --repository "${repository}" \
    --manifest "${manifest}" \
    --env-file "${environment_file}"

python3 "${readiness_waiter}" \
    --container "${candidate_container}" \
    --health-url "${health_url}" \
    --expected-image-id "${expected_image_id}" \
    --initial-restart-count "${initial_restart_count}" \
    --policy-file "${manifest}" \
    --result "${readiness_result}" \
    --failure-artifact "${failure_artifact}"
