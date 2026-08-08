#!/usr/bin/env bash

set -euo pipefail

PATH="${MARKET_DATA_WRAPPER_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
export PATH

LOCK_FILE="${MARKET_DATA_FGIS_LOCK_FILE:-/run/lock/fgis_yearly_update.lock}"
CONTAINER_NAME="${MARKET_DATA_SPREAD_CONTAINER:-spread-dashboard}"
RUNTIME_ROOT="${MARKET_DATA_FGIS_RUNTIME_HOST_ROOT:-/home/ubuntu/market-data}"
SOURCE_ROOT="${MARKET_DATA_FGIS_SOURCE_ROOT:-/home/ubuntu/market-data-runtime/fgis-yearly}"
LOG_ROOT="${MARKET_DATA_FGIS_LOG_ROOT:-/home/ubuntu/market-data-runtime/soybean-exports/logs}"
LOG_FILE="${MARKET_DATA_FGIS_LOG_FILE:-${LOG_ROOT}/fgis_yearly_update.log}"
CONTAINER_ENTRYPOINT="${MARKET_DATA_FGIS_ENTRYPOINT:-/app/04_scripts/soybean_exports/run_fgis_export_inspections.py}"
CALENDAR_YEAR="${MARKET_DATA_FGIS_CALENDAR_YEAR:-$(date -u +%Y)}"
MAX_DOWNLOAD_ATTEMPTS="${MARKET_DATA_FGIS_MAX_DOWNLOAD_ATTEMPTS:-10}"
MAX_NO_PROGRESS_ATTEMPTS=2
RANGE_CHUNK_BYTES=1048576
YEARLY_BASE_URL="https://fgisonline.ams.usda.gov/exportgrainreport"

cleanup() {
    unset MARKET_DATA_GIT_HEAD CONTAINER_IMAGE_ID OCI_REVISION
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

fail() {
    echo "FGIS Yearly update failed: $*" >&2
    exit 1
}

for required_command in awk chmod curl date docker flock mkdir mv python3 sha256sum stat touch wc; do
    command -v "${required_command}" >/dev/null 2>&1 || \
        fail "missing required command ${required_command}"
done

umask 077
mkdir -p "${LOG_ROOT}" || fail "cannot create FGIS log directory"
chmod 0700 "${LOG_ROOT}" || fail "cannot protect FGIS log directory"
touch "${LOG_FILE}" || fail "cannot create FGIS log file"
chmod 0600 "${LOG_FILE}" || fail "cannot protect FGIS log file"
exec >>"${LOG_FILE}" 2>&1
echo "FGIS Yearly wrapper started at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

[[ "${CALENDAR_YEAR}" =~ ^[0-9]{4}$ ]] || fail "calendar year must have four digits"
(( 10#${CALENDAR_YEAR} >= 1983 )) || fail "calendar year is outside the FGIS Yearly range"
[[ "${MAX_DOWNLOAD_ATTEMPTS}" =~ ^[0-9]+$ ]] || fail "download attempts must be an integer"
(( MAX_DOWNLOAD_ATTEMPTS >= 1 && MAX_DOWNLOAD_ATTEMPTS <= 10 )) || \
    fail "download attempts must be between 1 and 10"

SOURCE_FILE="CY${CALENDAR_YEAR}.csv"
OFFICIAL_URL="${YEARLY_BASE_URL}/${SOURCE_FILE}"
STAGING_DIR="${SOURCE_ROOT}/staging/${SOURCE_FILE%.csv}"
PARTIAL_FILE="${STAGING_DIR}/${SOURCE_FILE}.downloading"
FINAL_FILE="${STAGING_DIR}/${SOURCE_FILE}"
METADATA_FILE="${STAGING_DIR}/${SOURCE_FILE}.metadata.json"
METADATA_PARTIAL="${METADATA_FILE}.downloading"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
IDENTITY_FILE="${STAGING_DIR}/download.identity"

reject_partial() {
    local reason="$1"
    if [[ -f "${PARTIAL_FILE}" ]]; then
        mv "${PARTIAL_FILE}" "${PARTIAL_FILE}.rejected.${RUN_ID}.${reason}" || \
            fail "cannot quarantine rejected partial download"
    fi
}

mkdir -p "${STAGING_DIR}" || fail "cannot create controlled staging directory"
[[ -d "${RUNTIME_ROOT}/01_data" ]] || fail "runtime 01_data directory does not exist"

exec 9>"${LOCK_FILE}" || fail "cannot open update lock"
if ! flock -n 9; then
    echo "FGIS Yearly update already has a running instance; this run was skipped."
    exit 0
fi

if ! container_running="$(
    docker inspect --format '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null
)"; then
    fail "configured spread container does not exist"
fi
[[ "${container_running}" == "true" ]] || fail "configured spread container is not running"

if ! MARKET_DATA_GIT_HEAD="$(
    docker exec "${CONTAINER_NAME}" printenv MARKET_DATA_GIT_HEAD 2>/dev/null
)"; then
    fail "cannot read runtime Git identity"
fi
runtime_identity_lines="$(
    docker exec "${CONTAINER_NAME}" printenv MARKET_DATA_GIT_HEAD 2>/dev/null | wc -l
)"
[[ "${runtime_identity_lines}" == "1" && "${MARKET_DATA_GIT_HEAD}" =~ ^[0-9a-fA-F]{40}$ ]] || \
    fail "runtime Git identity is missing or invalid"
MARKET_DATA_GIT_HEAD="${MARKET_DATA_GIT_HEAD,,}"

if ! CONTAINER_IMAGE_ID="$(
    docker inspect --format '{{.Image}}' "${CONTAINER_NAME}" 2>/dev/null
)"; then
    fail "cannot resolve the configured spread image identity"
fi
[[ "${CONTAINER_IMAGE_ID}" =~ ^sha256:[0-9a-f]{64}$ ]] || \
    fail "configured spread image identity is invalid"

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

header_status() {
    awk '/^HTTP\// { code=$2 } END { print code }' "$1"
}

header_value() {
    local header_name="$1"
    local header_file="$2"
    awk -v wanted="${header_name}" '
        BEGIN { IGNORECASE=1 }
        $0 ~ "^" wanted ":[[:space:]]*" {
            sub("^[^:]+:[[:space:]]*", "")
            sub("\\r$", "")
            value=$0
        }
        END { print value }
    ' "${header_file}"
}

read_metadata() {
    local label="$1"
    local headers="${STAGING_DIR}/metadata.${label}.${RUN_ID}.headers"
    local errors="${STAGING_DIR}/metadata.${label}.${RUN_ID}.stderr"
    if ! curl --silent --show-error --fail --location --http1.1 \
        --connect-timeout 20 --max-time 120 --head \
        --dump-header "${headers}" --output /dev/null \
        "${OFFICIAL_URL}" 2>"${errors}"; then
        fail "official metadata request ${label} failed"
    fi
    local status
    status="$(header_status "${headers}")"
    [[ "${status}" == "200" ]] || fail "official metadata returned HTTP ${status:-unknown}"

    METADATA_CONTENT_LENGTH="$(header_value 'Content-Length' "${headers}")"
    METADATA_LAST_MODIFIED="$(header_value 'Last-Modified' "${headers}")"
    METADATA_ETAG="$(header_value 'ETag' "${headers}")"
    METADATA_ACCEPT_RANGES="$(header_value 'Accept-Ranges' "${headers}")"
    [[ "${METADATA_CONTENT_LENGTH}" =~ ^[0-9]+$ ]] || \
        fail "official Content-Length is missing or invalid"
    (( METADATA_CONTENT_LENGTH > 0 )) || fail "official Content-Length is zero"
    [[ -n "${METADATA_LAST_MODIFIED}" ]] || fail "official Last-Modified is missing"
}

read_metadata before
CONTENT_LENGTH_BEFORE="${METADATA_CONTENT_LENGTH}"
LAST_MODIFIED_BEFORE="${METADATA_LAST_MODIFIED}"
ETAG_BEFORE="${METADATA_ETAG}"
ACCEPT_RANGES_BEFORE_HEADER="${METADATA_ACCEPT_RANGES,,}"

expected_identity="${CONTENT_LENGTH_BEFORE}"$'\t'"${LAST_MODIFIED_BEFORE}"
if [[ -f "${PARTIAL_FILE}" ]]; then
    if [[ ! -f "${IDENTITY_FILE}" ]] || \
        [[ "$(<"${IDENTITY_FILE}")" != "${expected_identity}" ]] || \
        (( $(stat --format='%s' "${PARTIAL_FILE}") >= CONTENT_LENGTH_BEFORE )); then
        mv "${PARTIAL_FILE}" "${PARTIAL_FILE}.rejected.${RUN_ID}" || \
            fail "cannot quarantine invalid partial download"
    fi
fi
printf '%s' "${expected_identity}" >"${IDENTITY_FILE}" || fail "cannot record source identity"

attempt=0
no_progress_attempts=0
while true; do
    current_size=0
    [[ -f "${PARTIAL_FILE}" ]] && current_size="$(stat --format='%s' "${PARTIAL_FILE}")"
    (( current_size < CONTENT_LENGTH_BEFORE )) || break
    (( attempt < MAX_DOWNLOAD_ATTEMPTS )) || fail "download attempt limit reached"

    attempt=$((attempt + 1))
    before_size="${current_size}"
    headers="${STAGING_DIR}/download.${RUN_ID}.${attempt}.headers"
    errors="${STAGING_DIR}/download.${RUN_ID}.${attempt}.stderr"
    curl_exit=0
    if (( before_size == 0 )); then
        range_end=$((RANGE_CHUNK_BYTES - 1))
        (( range_end < CONTENT_LENGTH_BEFORE )) || range_end=$((CONTENT_LENGTH_BEFORE - 1))
        curl --silent --show-error --location --http1.1 --fail \
            --connect-timeout 20 --max-time 180 --speed-limit 1 --speed-time 30 \
            --range "0-${range_end}" --dump-header "${headers}" \
            --output "${PARTIAL_FILE}" "${OFFICIAL_URL}" 2>"${errors}" || curl_exit=$?
    else
        curl --silent --show-error --location --http1.1 --fail \
            --connect-timeout 20 --max-time 180 --speed-limit 1 --speed-time 30 \
            -C - --dump-header "${headers}" --output "${PARTIAL_FILE}" \
            "${OFFICIAL_URL}" 2>"${errors}" || curl_exit=$?
    fi

    after_size=0
    [[ -f "${PARTIAL_FILE}" ]] && after_size="$(stat --format='%s' "${PARTIAL_FILE}")"
    status="$(header_status "${headers}")"
    [[ -z "${status}" || "${status}" == "206" ]] || \
        fail "Range download returned HTTP ${status}"
    if (( after_size > CONTENT_LENGTH_BEFORE )); then
        reject_partial "oversize"
        fail "partial exceeds official Content-Length"
    fi

    if (( after_size > before_size )); then
        [[ "${status}" == "206" ]] || fail "Range response did not prove HTTP 206"
        no_progress_attempts=0
        echo "FGIS Range attempt ${attempt}: bytes=${after_size}/${CONTENT_LENGTH_BEFORE}, curl_exit=${curl_exit}"
    else
        no_progress_attempts=$((no_progress_attempts + 1))
        echo "FGIS Range attempt ${attempt}: no progress (${no_progress_attempts}/${MAX_NO_PROGRESS_ATTEMPTS}), curl_exit=${curl_exit}" >&2
        (( no_progress_attempts < MAX_NO_PROGRESS_ATTEMPTS )) || \
            fail "download made no progress for ${MAX_NO_PROGRESS_ATTEMPTS} consecutive attempts"
    fi
done

[[ -f "${PARTIAL_FILE}" ]] || fail "completed source file is absent"
actual_size="$(stat --format='%s' "${PARTIAL_FILE}")"
(( actual_size == CONTENT_LENGTH_BEFORE )) || fail "completed source length mismatch"

export PARTIAL_FILE CALENDAR_YEAR
if ! python3 - <<'PY'
import csv
import os
from datetime import datetime
from pathlib import Path

path = Path(os.environ["PARTIAL_FILE"])
year = int(os.environ["CALENDAR_YEAR"])
content = path.read_bytes()
text = content.decode("utf-8", errors="strict")
if "<html" in text[:4096].lower() or "<!doctype html" in text[:4096].lower():
    raise SystemExit("source is an HTML response")
reader = csv.DictReader(text.splitlines())
required = {"Thursday", "Cert Date", "Grain", "Destination", "Metric Ton"}
missing = sorted(required - set(reader.fieldnames or ()))
if missing:
    raise SystemExit("missing required CSV fields: " + ", ".join(missing))
soybeans = 0
for row in reader:
    if row.get("Grain") != "SOYBEANS":
        continue
    cert_date = datetime.strptime(row["Cert Date"], "%Y%m%d").date()
    if cert_date.year != year:
        raise SystemExit("SOYBEANS Cert Date does not match calendar year")
    soybeans += 1
if soybeans == 0:
    raise SystemExit("source has no exact SOYBEANS rows")
PY
then
    reject_partial "integrity"
    fail "host source integrity validation failed"
fi

read_metadata after
CONTENT_LENGTH_AFTER="${METADATA_CONTENT_LENGTH}"
LAST_MODIFIED_AFTER="${METADATA_LAST_MODIFIED}"
ETAG_AFTER="${METADATA_ETAG}"
ACCEPT_RANGES_AFTER_HEADER="${METADATA_ACCEPT_RANGES,,}"
ACCEPT_RANGES_BEFORE="bytes"
ACCEPT_RANGES_AFTER="bytes"

if (( CONTENT_LENGTH_AFTER != CONTENT_LENGTH_BEFORE )); then
    reject_partial "metadata-change"
    fail "official Content-Length changed during download"
fi
before_epoch="$(date -u -d "${LAST_MODIFIED_BEFORE}" +%s 2>/dev/null)" || \
    fail "cannot parse pre-download Last-Modified"
after_epoch="$(date -u -d "${LAST_MODIFIED_AFTER}" +%s 2>/dev/null)" || \
    fail "cannot parse post-download Last-Modified"
last_modified_delta=$((after_epoch - before_epoch))
(( last_modified_delta < 0 )) && last_modified_delta=$((-last_modified_delta))
if (( last_modified_delta > 1 )); then
    reject_partial "metadata-change"
    fail "official Last-Modified changed during download"
fi

SOURCE_SHA256="$(sha256sum "${PARTIAL_FILE}" | awk '{print $1}')"
[[ "${SOURCE_SHA256}" =~ ^[0-9a-f]{64}$ ]] || fail "source SHA-256 is invalid"
DOWNLOAD_COMPLETED_AT_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

export SOURCE_FILE OFFICIAL_URL CONTENT_LENGTH_BEFORE LAST_MODIFIED_BEFORE ETAG_BEFORE
export ACCEPT_RANGES_BEFORE CONTENT_LENGTH_AFTER LAST_MODIFIED_AFTER ETAG_AFTER
export ACCEPT_RANGES_AFTER ACCEPT_RANGES_BEFORE_HEADER ACCEPT_RANGES_AFTER_HEADER
export SOURCE_SHA256 DOWNLOAD_COMPLETED_AT_UTC METADATA_PARTIAL
python3 - <<'PY' || fail "cannot create source metadata sidecar"
import json
import os
from pathlib import Path

def optional(name: str):
    return os.environ.get(name) or None

payload = {
    "schema_version": 1,
    "source_authority": "USDA FGIS",
    "source_channel": "yearly_export_grain_csv",
    "calendar_year": int(os.environ["CALENDAR_YEAR"]),
    "source_file": os.environ["SOURCE_FILE"],
    "official_url": os.environ["OFFICIAL_URL"],
    "source_sha256": os.environ["SOURCE_SHA256"],
    "source_size": int(os.environ["CONTENT_LENGTH_BEFORE"]),
    "content_length": int(os.environ["CONTENT_LENGTH_BEFORE"]),
    "accept_ranges": os.environ["ACCEPT_RANGES_BEFORE"],
    "last_modified": os.environ["LAST_MODIFIED_BEFORE"],
    "etag": optional("ETAG_BEFORE"),
    "download_completed_at_utc": os.environ["DOWNLOAD_COMPLETED_AT_UTC"],
    "metadata_before": {
        "content_length": int(os.environ["CONTENT_LENGTH_BEFORE"]),
        "last_modified": os.environ["LAST_MODIFIED_BEFORE"],
        "etag": optional("ETAG_BEFORE"),
        "accept_ranges": os.environ["ACCEPT_RANGES_BEFORE"],
        "accept_ranges_header": optional("ACCEPT_RANGES_BEFORE_HEADER"),
    },
    "metadata_after": {
        "content_length": int(os.environ["CONTENT_LENGTH_AFTER"]),
        "last_modified": os.environ["LAST_MODIFIED_AFTER"],
        "etag": optional("ETAG_AFTER"),
        "accept_ranges": os.environ["ACCEPT_RANGES_AFTER"],
        "accept_ranges_header": optional("ACCEPT_RANGES_AFTER_HEADER"),
    },
}
Path(os.environ["METADATA_PARTIAL"]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

# ETag is retained for audit only: different USDA backends may emit different ETags.
# Complete length, strict CSV validation, and SHA-256 are the promotion identity.
mv -f "${PARTIAL_FILE}" "${FINAL_FILE}" || fail "cannot atomically promote source CSV"
mv -f "${METADATA_PARTIAL}" "${METADATA_FILE}" || fail "cannot atomically promote metadata"

PIPELINE_STDOUT="${STAGING_DIR}/pipeline.${RUN_ID}.stdout"
PIPELINE_STDERR="${STAGING_DIR}/pipeline.${RUN_ID}.stderr"
pipeline_exit=0
docker run --rm --network none \
    --env "MARKET_DATA_GIT_HEAD=${MARKET_DATA_GIT_HEAD}" \
    --mount "type=bind,src=${STAGING_DIR},dst=/source,readonly" \
    --mount "type=bind,src=${RUNTIME_ROOT}/01_data,dst=/runtime/01_data" \
    --entrypoint python \
    "${CONTAINER_IMAGE_ID}" \
    "${CONTAINER_ENTRYPOINT}" \
    --runtime-root /runtime \
    --source yearly \
    --source-file "/source/${SOURCE_FILE}" \
    --source-metadata-file "/source/${SOURCE_FILE}.metadata.json" \
    "$@" >"${PIPELINE_STDOUT}" 2>"${PIPELINE_STDERR}" || pipeline_exit=$?

if ! printf '%s\n' "${pipeline_exit}" >"${STAGING_DIR}/pipeline.${RUN_ID}.exit_code"; then
    echo "FGIS container pipeline exit status could not be recorded." >&2
    if (( pipeline_exit != 0 )); then
        exit "${pipeline_exit}"
    fi
    fail "cannot record successful pipeline exit status"
fi
if (( pipeline_exit != 0 )); then
    echo "FGIS container pipeline failed with exit code ${pipeline_exit}; stable was not promoted." >&2
    exit "${pipeline_exit}"
fi
echo "FGIS container pipeline completed successfully."
exit 0
