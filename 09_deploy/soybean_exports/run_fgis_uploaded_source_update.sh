#!/usr/bin/env bash

set -euo pipefail

PATH="${MARKET_DATA_WRAPPER_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
export PATH

LOCK_FILE="${MARKET_DATA_FGIS_LOCK_FILE:-/run/lock/fgis_yearly_update.lock}"
CONTAINER_NAME="${MARKET_DATA_SPREAD_CONTAINER:-spread-dashboard}"
RUNTIME_ROOT="${MARKET_DATA_FGIS_RUNTIME_HOST_ROOT:-/home/ubuntu/market-data}"
SOURCE_ROOT="${MARKET_DATA_FGIS_SOURCE_ROOT:-/home/ubuntu/market-data-runtime/fgis-yearly}"
INBOX_DIR="${MARKET_DATA_FGIS_UPLOAD_INBOX:-${SOURCE_ROOT}/inbox}"
ACCEPTED_ROOT="${MARKET_DATA_FGIS_ACCEPTED_ROOT:-${SOURCE_ROOT}/manual-sources}"
LOG_ROOT="${MARKET_DATA_FGIS_LOG_ROOT:-/home/ubuntu/market-data-runtime/soybean-exports/logs}"
LOG_FILE="${MARKET_DATA_FGIS_UPLOAD_LOG_FILE:-${LOG_ROOT}/fgis_uploaded_source_update.log}"
CONTAINER_ENTRYPOINT="${MARKET_DATA_FGIS_ENTRYPOINT:-/app/04_scripts/soybean_exports/run_fgis_export_inspections.py}"
CALENDAR_YEAR="${MARKET_DATA_FGIS_CALENDAR_YEAR:-$(date -u +%Y)}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STAGING_DIR=""

fail() {
    local message="$*"
    if [[ -n "${STAGING_DIR}" && -d "${STAGING_DIR}" ]]; then
        mkdir -p "${ACCEPTED_ROOT}/rejected"
        chmod 0700 "${ACCEPTED_ROOT}/rejected"
        mv "${STAGING_DIR}" "${ACCEPTED_ROOT}/rejected/failed-${RUN_ID}" || true
        STAGING_DIR=""
    fi
    echo "FGIS uploaded source update failed: ${message}" >&2
    exit 1
}

cleanup() {
    unset MARKET_DATA_GIT_HEAD CONTAINER_IMAGE_ID OCI_REVISION
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for required_command in awk chmod date docker flock mkdir mv python3 sha256sum stat touch; do
    command -v "${required_command}" >/dev/null 2>&1 || \
        fail "missing required command ${required_command}"
done

umask 077
mkdir -p "${LOG_ROOT}" || fail "cannot create FGIS upload log directory"
chmod 0700 "${LOG_ROOT}" || fail "cannot protect FGIS upload log directory"
touch "${LOG_FILE}" || fail "cannot create FGIS upload log file"
chmod 0600 "${LOG_FILE}" || fail "cannot protect FGIS upload log file"
exec >>"${LOG_FILE}" 2>&1
echo "FGIS uploaded source wrapper started at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

[[ "${CALENDAR_YEAR}" =~ ^[0-9]{4}$ ]] || fail "calendar year must have four digits"
(( 10#${CALENDAR_YEAR} >= 1983 )) || fail "calendar year is outside the FGIS Yearly range"
[[ -d "${RUNTIME_ROOT}/01_data" ]] || fail "runtime 01_data directory does not exist"

exec 9>"${LOCK_FILE}" || fail "cannot open FGIS update lock"
if ! flock -n 9; then
    echo "FGIS update already has a running instance; this upload was skipped."
    exit 0
fi

SOURCE_FILE="CY${CALENDAR_YEAR}.csv"
UPLOAD_SOURCE="${INBOX_DIR}/${SOURCE_FILE}.uploading"
UPLOAD_METADATA="${INBOX_DIR}/${SOURCE_FILE}.metadata.json.uploading"
mkdir -p "${INBOX_DIR}" "${ACCEPTED_ROOT}" "${ACCEPTED_ROOT}/${SOURCE_FILE%.csv}"
chmod 0700 "${INBOX_DIR}" "${ACCEPTED_ROOT}" "${ACCEPTED_ROOT}/${SOURCE_FILE%.csv}"
[[ -f "${UPLOAD_SOURCE}" && ! -L "${UPLOAD_SOURCE}" ]] || \
    fail "uploaded FGIS source is absent or invalid"
[[ -f "${UPLOAD_METADATA}" && ! -L "${UPLOAD_METADATA}" ]] || \
    fail "uploaded FGIS metadata is absent or invalid"

STAGING_DIR="${ACCEPTED_ROOT}/${SOURCE_FILE%.csv}/.validating-${RUN_ID}"
mkdir "${STAGING_DIR}" || fail "cannot create FGIS upload validation directory"
chmod 0700 "${STAGING_DIR}"
mv "${UPLOAD_SOURCE}" "${STAGING_DIR}/${SOURCE_FILE}" || fail "cannot stage uploaded FGIS source"
mv "${UPLOAD_METADATA}" "${STAGING_DIR}/${SOURCE_FILE}.metadata.json" || \
    fail "cannot stage uploaded FGIS metadata"

SOURCE_SHA256="$(sha256sum "${STAGING_DIR}/${SOURCE_FILE}" | awk '{print $1}')"
SOURCE_SIZE="$(stat --format='%s' "${STAGING_DIR}/${SOURCE_FILE}")"
[[ "${SOURCE_SHA256}" =~ ^[0-9a-f]{64}$ ]] || fail "uploaded source SHA-256 is invalid"
(( SOURCE_SIZE > 0 )) || fail "uploaded source is empty"

export SOURCE_PATH="${STAGING_DIR}/${SOURCE_FILE}"
export METADATA_PATH="${STAGING_DIR}/${SOURCE_FILE}.metadata.json"
export SOURCE_FILE CALENDAR_YEAR SOURCE_SHA256 SOURCE_SIZE RUN_ID
python3 - <<'PY' || fail "uploaded source metadata validation failed"
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path

source_path = Path(os.environ["SOURCE_PATH"])
metadata_path = Path(os.environ["METADATA_PATH"])
metadata_bytes = metadata_path.read_bytes()
metadata = json.loads(metadata_bytes.decode("utf-8", errors="strict"))
if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
    raise SystemExit("metadata schema is invalid")

year = int(os.environ["CALENDAR_YEAR"])
source_file = os.environ["SOURCE_FILE"]
source_size = int(os.environ["SOURCE_SIZE"])
source_sha256 = os.environ["SOURCE_SHA256"]
expected_url = f"https://fgisonline.ams.usda.gov/exportgrainreport/{source_file}"
required = {
    "source_authority": "USDA FGIS",
    "source_channel": "yearly_export_grain_csv",
    "calendar_year": year,
    "source_file": source_file,
    "official_url": expected_url,
    "source_size": source_size,
    "content_length": source_size,
    "source_sha256": source_sha256,
    "accept_ranges": "bytes",
}
for name, expected in required.items():
    if metadata.get(name) != expected:
        raise SystemExit(f"metadata {name} identity mismatch")
if not isinstance(metadata.get("last_modified"), str) or not metadata["last_modified"].strip():
    raise SystemExit("metadata Last-Modified is absent")
downloaded_at = metadata.get("download_completed_at_utc")
if not isinstance(downloaded_at, str) or not downloaded_at.strip():
    raise SystemExit("metadata download time is absent")
for name in ("metadata_before", "metadata_after"):
    observed = metadata.get(name)
    if not isinstance(observed, dict):
        raise SystemExit(f"metadata {name} is absent")
    if observed.get("content_length") != source_size or observed.get("accept_ranges") != "bytes":
        raise SystemExit(f"metadata {name} identity mismatch")

metadata["source_filename"] = source_file
metadata["downloaded_at"] = downloaded_at
metadata["uploaded_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
    "+00:00", "Z"
)
metadata["upload_metadata_sha256"] = hashlib.sha256(metadata_bytes).hexdigest()
temporary = metadata_path.with_name(metadata_path.name + f".{os.environ['RUN_ID']}.validated")
temporary.write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
temporary.chmod(0o400)
os.replace(temporary, metadata_path)
PY

chmod 0400 "${STAGING_DIR}/${SOURCE_FILE}" "${STAGING_DIR}/${SOURCE_FILE}.metadata.json"
RELEASE_DIR="${ACCEPTED_ROOT}/${SOURCE_FILE%.csv}/${SOURCE_SHA256}"
if [[ -e "${RELEASE_DIR}" ]]; then
    [[ -d "${RELEASE_DIR}" && ! -L "${RELEASE_DIR}" ]] || \
        fail "existing accepted source release is invalid"
    [[ "$(sha256sum "${RELEASE_DIR}/${SOURCE_FILE}" | awk '{print $1}')" == "${SOURCE_SHA256}" ]] || \
        fail "existing accepted source release SHA-256 differs"
    mkdir -p "${ACCEPTED_ROOT}/duplicates"
    chmod 0700 "${ACCEPTED_ROOT}/duplicates"
    mv "${STAGING_DIR}" "${ACCEPTED_ROOT}/duplicates/${RUN_ID}-${SOURCE_SHA256}" || \
        fail "cannot preserve duplicate uploaded source evidence"
    STAGING_DIR=""
    echo "uploaded source already has an accepted immutable release"
else
    mv "${STAGING_DIR}" "${RELEASE_DIR}" || fail "cannot atomically promote uploaded source"
    STAGING_DIR=""
    echo "uploaded source promoted sha256=${SOURCE_SHA256} size=${SOURCE_SIZE}"
fi

if ! container_running="$(
    docker inspect --format '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null
)"; then
    fail "configured spread container does not exist"
fi
[[ "${container_running}" == "true" ]] || fail "configured spread container is not running"

CONTAINER_IMAGE_ID="$(docker inspect --format '{{.Image}}' "${CONTAINER_NAME}" 2>/dev/null)" || \
    fail "cannot resolve the configured spread image identity"
[[ "${CONTAINER_IMAGE_ID}" =~ ^sha256:[0-9a-f]{64}$ ]] || \
    fail "configured spread image identity is invalid"
MARKET_DATA_GIT_HEAD="$(docker exec "${CONTAINER_NAME}" printenv MARKET_DATA_GIT_HEAD 2>/dev/null)" || \
    fail "cannot read runtime Git identity"
[[ "${MARKET_DATA_GIT_HEAD}" =~ ^[0-9a-fA-F]{40}$ ]] || \
    fail "runtime Git identity is missing or invalid"
MARKET_DATA_GIT_HEAD="${MARKET_DATA_GIT_HEAD,,}"
OCI_REVISION="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "${CONTAINER_IMAGE_ID}" 2>/dev/null)" || \
    fail "cannot read formal image OCI revision"
[[ "${OCI_REVISION}" =~ ^[0-9a-fA-F]{40}$ ]] || fail "formal image OCI revision is invalid"
OCI_REVISION="${OCI_REVISION,,}"
[[ "${OCI_REVISION}" == "${MARKET_DATA_GIT_HEAD}" ]] || \
    fail "formal image OCI revision and runtime Git identity disagree"

export MARKET_DATA_GIT_HEAD
pipeline_exit=0
docker run --rm --network none \
    --env "MARKET_DATA_GIT_HEAD=${MARKET_DATA_GIT_HEAD}" \
    --mount "type=bind,src=${RELEASE_DIR},dst=/source,readonly" \
    --mount "type=bind,src=${RUNTIME_ROOT}/01_data,dst=/runtime/01_data" \
    --entrypoint python \
    "${CONTAINER_IMAGE_ID}" \
    "${CONTAINER_ENTRYPOINT}" \
    --runtime-root /runtime \
    --source yearly \
    --source-file "/source/${SOURCE_FILE}" \
    --source-metadata-file "/source/${SOURCE_FILE}.metadata.json" \
    "$@" || pipeline_exit=$?

if (( pipeline_exit != 0 )); then
    echo "FGIS uploaded source pipeline failed with exit code ${pipeline_exit}; stable was not promoted." >&2
    exit "${pipeline_exit}"
fi
echo "FGIS uploaded source pipeline completed successfully."
exit 0
