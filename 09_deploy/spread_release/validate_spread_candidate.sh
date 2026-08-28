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
server_store_result="$(dirname -- "${readiness_result}")/candidate_server_store_contract.json"

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

# Runtime data provenance is a precondition for readiness: a healthy Streamlit
# process must never validate while silently consuming loose /app/01_data files.
python3 - "${script_dir}" "${candidate_container}" "${server_store_result}" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from release_contract import validate_candidate_server_store_runtime

evidence = validate_candidate_server_store_runtime(sys.argv[2])
target = Path(sys.argv[3])
with target.open("x", encoding="utf-8", newline="\n") as handle:
    json.dump(evidence, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY

python3 "${readiness_waiter}" \
    --container "${candidate_container}" \
    --health-url "${health_url}" \
    --expected-image-id "${expected_image_id}" \
    --initial-restart-count "${initial_restart_count}" \
    --policy-file "${manifest}" \
    --result "${readiness_result}" \
    --failure-artifact "${failure_artifact}"
