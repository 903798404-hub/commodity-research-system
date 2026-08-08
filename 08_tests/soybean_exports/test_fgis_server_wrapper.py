from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
WRAPPER = REPOSITORY / "09_deploy/soybean_exports/run_fgis_yearly_update.sh"
RUNTIME_GIT_HEAD = "c07f2635a86a1c4f3e13167a982e5bb56e391b91"
IMAGE_ID = "sha256:" + "b" * 64
LAST_MODIFIED = "Sat, 08 Aug 2026 08:00:00 GMT"


def _bash_executable() -> Path:
    discovered = shutil.which("bash")
    candidates = [
        Path(discovered) if discovered else None,
        Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        / "Git"
        / "bin"
        / "bash.exe",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    raise AssertionError("Bash is required to validate the FGIS host wrapper")


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return resolved.as_posix()
    posix = resolved.as_posix()
    drive, remainder = posix.split(":", maxsplit=1)
    return f"/{drive.lower()}{remainder}"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _fgis_environment(
    tmp_path: Path,
    *,
    total_size: int,
    responses: list[dict[str, object]],
    partial: bytes | None = None,
    source_body: bytes | None = None,
    max_attempts: int = 16,
) -> tuple[dict[str, str], dict[str, Path]]:
    mock_bin = tmp_path / "mock-bin"
    runtime = tmp_path / "runtime"
    source_root = tmp_path / "source"
    staging = source_root / "staging" / "CY2026"
    logs = tmp_path / "logs"
    records = tmp_path / "records"
    mock_bin.mkdir()
    staging.mkdir(parents=True)
    records.mkdir()
    stable = (
        runtime
        / "01_data"
        / "processed"
        / "soybean_export_inspections"
        / "soybean_export_inspections_weekly.parquet"
    )
    stable.parent.mkdir(parents=True)
    stable.write_bytes(b"existing-stable-must-not-change")
    partial_path = staging / "CY2026.csv.downloading"
    if partial is not None:
        partial_path.write_bytes(partial)
        (staging / "download.identity").write_text(
            f"{total_size}\t{LAST_MODIFIED}", encoding="utf-8", newline=""
        )
    source_body_path = records / "source-body.bin"
    if source_body is not None:
        source_body_path.write_bytes(source_body)
    plan_path = records / "plan.json"
    plan_path.write_text(
        json.dumps({"total_size": total_size, "responses": responses}),
        encoding="utf-8",
    )
    state_path = records / "range-state.txt"
    driver_path = records / "curl_driver.py"
    driver_path.write_text(
        r'''from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def native(value: str) -> Path:
    if os.name == "nt" and re.match(r"^/[A-Za-z]/", value):
        value = value[1].upper() + ":" + value[2:]
    return Path(value)


def option(arguments: list[str], name: str) -> str:
    return arguments[arguments.index(name) + 1]


arguments = sys.argv[1:]
plan = json.loads(native(os.environ["MOCK_CURL_PLAN"]).read_text(encoding="utf-8"))
headers = native(option(arguments, "--dump-header"))
output = option(arguments, "--output")
total = int(plan["total_size"])
if "--head" in arguments:
    headers.write_text(
        "HTTP/1.1 200 OK\r\n"
        f"Content-Length: {total}\r\n"
        f"Last-Modified: {os.environ['MOCK_LAST_MODIFIED']}\r\n"
        "ETag: fixture-etag\r\n"
        "Accept-Ranges: bytes\r\n\r\n",
        encoding="utf-8",
        newline="",
    )
    raise SystemExit(0)

requested_start, requested_end = map(int, option(arguments, "--range").split("-"))
requests = native(os.environ["MOCK_RANGE_REQUESTS"])
with requests.open("a", encoding="utf-8", newline="\n") as handle:
    handle.write(f"{requested_start}-{requested_end}\n")
state = native(os.environ["MOCK_CURL_STATE"])
index = int(state.read_text(encoding="utf-8")) if state.exists() else 0
responses = plan["responses"]
if index >= len(responses):
    raise SystemExit("curl fixture response plan exhausted")
response = responses[index]
state.write_text(str(index + 1), encoding="utf-8")
status = int(response.get("status", 206))
body_mode = response.get("body", "bytes")
if body_mode == "source":
    source = native(os.environ["MOCK_SOURCE_BODY"]).read_bytes()
    body = source[requested_start : requested_end + 1]
else:
    body = b"R" * int(response.get("body_size", requested_end - requested_start + 1))
native(output).write_bytes(body)
content_range = response.get("content_range")
if content_range == "auto" or (content_range is None and status == 206):
    content_range = f"bytes {requested_start}-{requested_start + len(body) - 1}/{total}"
header_text = f"HTTP/1.1 {status} fixture\r\n"
if content_range:
    header_text += f"Content-Range: {content_range}\r\n"
header_text += f"Content-Length: {len(body)}\r\n\r\n"
if not response.get("omit_headers", False):
    headers.write_text(header_text, encoding="utf-8", newline="")
raise SystemExit(int(response.get("curl_exit", 0)))
''',
        encoding="utf-8",
        newline="\n",
    )
    _write_executable(
        mock_bin / "curl",
        '#!/usr/bin/env bash\nexec "${MOCK_REAL_PYTHON}" "${MOCK_CURL_DRIVER}" "$@"\n',
    )
    _write_executable(mock_bin / "flock", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        mock_bin / "python3",
        '#!/usr/bin/env bash\nexec "${MOCK_REAL_PYTHON}" "$@"\n',
    )
    _write_executable(
        mock_bin / "docker",
        """#!/usr/bin/env bash
if [[ "$1" == "inspect" && "$*" == *".State.Running"* ]]; then
    printf 'true\n'
elif [[ "$1" == "inspect" && "$*" == *".Image"* ]]; then
    printf '%s\n' "${MOCK_IMAGE_ID}"
elif [[ "$1" == "exec" && "${3-}" == "printenv" ]]; then
    printf '%s\n' "${MOCK_RUNTIME_GIT_HEAD}"
elif [[ "$1" == "image" && "${2-}" == "inspect" ]]; then
    printf '%s\n' "${MOCK_RUNTIME_GIT_HEAD}"
elif [[ "$1" == "run" ]]; then
    printf '%s\n' "$@" >"${MOCK_DOCKER_RUN_ARGS}"
else
    exit 2
fi
""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "MARKET_DATA_WRAPPER_PATH": f"{_bash_path(mock_bin)}:/usr/bin:/bin",
            "MARKET_DATA_FGIS_LOCK_FILE": _bash_path(tmp_path / "fgis.lock"),
            "MARKET_DATA_FGIS_RUNTIME_HOST_ROOT": _bash_path(runtime),
            "MARKET_DATA_FGIS_SOURCE_ROOT": _bash_path(source_root),
            "MARKET_DATA_FGIS_LOG_ROOT": _bash_path(logs),
            "MARKET_DATA_FGIS_CALENDAR_YEAR": "2026",
            "MARKET_DATA_FGIS_MAX_DOWNLOAD_ATTEMPTS": str(max_attempts),
            "MOCK_REAL_PYTHON": _bash_path(Path(sys.executable)),
            "MOCK_CURL_DRIVER": _bash_path(driver_path),
            "MOCK_CURL_PLAN": _bash_path(plan_path),
            "MOCK_CURL_STATE": _bash_path(state_path),
            "MOCK_RANGE_REQUESTS": _bash_path(records / "range-requests.txt"),
            "MOCK_SOURCE_BODY": _bash_path(source_body_path),
            "MOCK_LAST_MODIFIED": LAST_MODIFIED,
            "MOCK_IMAGE_ID": IMAGE_ID,
            "MOCK_RUNTIME_GIT_HEAD": RUNTIME_GIT_HEAD,
            "MOCK_DOCKER_RUN_ARGS": _bash_path(records / "docker-run-args.txt"),
        }
    )
    return environment, {
        "staging": staging,
        "partial": partial_path,
        "final": staging / "CY2026.csv",
        "stable": stable,
        "log": logs / "fgis_yearly_update.log",
        "state": state_path,
        "requests": records / "range-requests.txt",
        "docker_args": records / "docker-run-args.txt",
    }


def _run_wrapper(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash_executable(), _bash_path(WRAPPER)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def _assert_no_range_temporaries(staging: Path) -> None:
    assert not list(staging.glob("*.chunk.*"))
    assert not list(staging.glob("*.append.*"))
    assert not list(staging.glob("download.*.headers"))


def test_fgis_wrapper_has_finite_range_resume_and_fail_closed_integrity_contract() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert "MAX_DOWNLOAD_ATTEMPTS" in source
    assert "<= 16" in source
    assert "MAX_NO_PROGRESS_ATTEMPTS=2" in source
    assert "--range" in source
    assert "-C -" not in source
    assert "${SOURCE_FILE}.chunk.${RUN_ID}.${attempt}" in source
    assert "range_ignored_http_200" in source
    assert "Content-Range" in source
    assert '"${status}" != "206"' in source
    assert "Content-Length" in source
    assert "Last-Modified" in source
    assert "Accept-Ranges" in source
    assert "metadata_before" in source
    assert "metadata_after" in source
    assert "sha256sum" in source
    assert '"source_size": int(os.environ["CONTENT_LENGTH_BEFORE"])' in source
    assert "decode(\"utf-8\", errors=\"strict\")" in source
    assert '{"Thursday", "Cert Date", "Grain", "Destination", "Metric Ton"}' in source
    assert 'row.get("Grain") != "SOYBEANS"' in source


def test_http_200_body_cannot_modify_existing_partial(tmp_path: Path) -> None:
    original = b"P" * 6_775_238
    original_sha = hashlib.sha256(original).hexdigest()
    environment, paths = _fgis_environment(
        tmp_path,
        total_size=7_227_679,
        partial=original,
        responses=[{"status": 200, "body_size": 2_491_043}],
        max_attempts=1,
    )

    result = _run_wrapper(environment)

    assert result.returncode == 1
    assert paths["partial"].stat().st_size == 6_775_238
    assert hashlib.sha256(paths["partial"].read_bytes()).hexdigest() == original_sha
    assert paths["stable"].read_bytes() == b"existing-stable-must-not-change"
    assert "range_ignored_http_200" in paths["log"].read_text(encoding="utf-8")
    _assert_no_range_temporaries(paths["staging"])


def test_valid_206_chunk_is_atomically_appended(tmp_path: Path) -> None:
    original = b"A" * 1_048_576
    environment, paths = _fgis_environment(
        tmp_path,
        total_size=7_227_679,
        partial=original,
        responses=[
            {
                "status": 206,
                "body_size": 1_048_576,
                "content_range": "bytes 1048576-2097151/7227679",
            }
        ],
        max_attempts=1,
    )

    result = _run_wrapper(environment)

    assert result.returncode == 1
    assert paths["partial"].stat().st_size == 2_097_152
    assert paths["partial"].read_bytes() == original + (b"R" * 1_048_576)
    assert paths["requests"].read_text(encoding="utf-8") == "1048576-2097151\n"
    _assert_no_range_temporaries(paths["staging"])


def test_http_200_at_zero_offset_is_discarded(tmp_path: Path) -> None:
    environment, paths = _fgis_environment(
        tmp_path,
        total_size=1_000_000,
        responses=[{"status": 200, "body_size": 1_000_000}],
        max_attempts=1,
    )

    result = _run_wrapper(environment)

    assert result.returncode == 1
    assert not paths["partial"].exists()
    assert "range_ignored_http_200" in paths["log"].read_text(encoding="utf-8")
    _assert_no_range_temporaries(paths["staging"])


def test_wrong_content_range_cannot_modify_partial(tmp_path: Path) -> None:
    original = b"B" * 1_048_576
    original_sha = hashlib.sha256(original).hexdigest()
    environment, paths = _fgis_environment(
        tmp_path,
        total_size=7_227_679,
        partial=original,
        responses=[
            {
                "status": 206,
                "body_size": 1_048_576,
                "content_range": "bytes 0-1048575/7227679",
            }
        ],
        max_attempts=1,
    )

    result = _run_wrapper(environment)

    assert result.returncode == 1
    assert hashlib.sha256(paths["partial"].read_bytes()).hexdigest() == original_sha
    assert "content_range_or_chunk_size_mismatch" in paths["log"].read_text(
        encoding="utf-8"
    )
    _assert_no_range_temporaries(paths["staging"])


def test_content_range_chunk_size_mismatch_cannot_modify_partial(
    tmp_path: Path,
) -> None:
    original = b"E" * 1_048_576
    original_sha = hashlib.sha256(original).hexdigest()
    environment, paths = _fgis_environment(
        tmp_path,
        total_size=7_227_679,
        partial=original,
        responses=[
            {
                "status": 206,
                "body_size": 524_288,
                "content_range": "bytes 1048576-2097151/7227679",
            }
        ],
        max_attempts=1,
    )

    result = _run_wrapper(environment)

    assert result.returncode == 1
    assert hashlib.sha256(paths["partial"].read_bytes()).hexdigest() == original_sha
    assert "content_range_or_chunk_size_mismatch" in paths["log"].read_text(
        encoding="utf-8"
    )
    _assert_no_range_temporaries(paths["staging"])


def test_interrupted_chunk_cannot_modify_partial(tmp_path: Path) -> None:
    original = b"C" * 1_048_576
    original_sha = hashlib.sha256(original).hexdigest()
    environment, paths = _fgis_environment(
        tmp_path,
        total_size=7_227_679,
        partial=original,
        responses=[
            {
                "status": 206,
                "body_size": 524_288,
                "content_range": "auto",
                "curl_exit": 18,
                "omit_headers": True,
            }
        ],
        max_attempts=1,
    )

    result = _run_wrapper(environment)

    assert result.returncode == 1
    assert hashlib.sha256(paths["partial"].read_bytes()).hexdigest() == original_sha
    assert "range_transport_failure_curl_18" in paths["log"].read_text(encoding="utf-8")
    _assert_no_range_temporaries(paths["staging"])


def test_multiple_validated_chunks_complete_before_atomic_source_promotion(
    tmp_path: Path,
) -> None:
    header = b"Thursday,Cert Date,Grain,Destination,Metric Ton\n"
    row = b"2026-08-06,20260806,SOYBEANS,CHINA T,1\n"
    source_body = header + (row * 110_000)
    chunk_count = (len(source_body) + 1_048_575) // 1_048_576
    responses: list[dict[str, object]] = [
        {"status": 206, "body": "source", "content_range": "auto"},
        {"status": 206, "body": "source", "content_range": "auto"},
        {"status": 200, "body_size": 2_491_043},
    ]
    responses.extend(
        {"status": 206, "body": "source", "content_range": "auto"}
        for _ in range(chunk_count - 2)
    )
    environment, paths = _fgis_environment(
        tmp_path,
        total_size=len(source_body),
        source_body=source_body,
        responses=responses,
    )

    result = _run_wrapper(environment)

    assert result.returncode == 0, paths["log"].read_text(encoding="utf-8")
    assert paths["final"].read_bytes() == source_body
    assert not paths["partial"].exists()
    assert int(paths["state"].read_text(encoding="utf-8")) == chunk_count + 1
    requested_ranges = paths["requests"].read_text(encoding="utf-8").splitlines()
    assert requested_ranges[:2] == ["0-1048575", "1048576-2097151"]
    assert requested_ranges[2] == requested_ranges[3] == "2097152-3145727"
    assert requested_ranges[-1] == f"4194304-{len(source_body) - 1}"
    assert "range_ignored_http_200" in paths["log"].read_text(encoding="utf-8")
    assert "--network\nnone\n" in paths["docker_args"].read_text(encoding="utf-8")
    assert paths["stable"].read_bytes() == b"existing-stable-must-not-change"
    _assert_no_range_temporaries(paths["staging"])


def test_consecutive_unvalidated_responses_fail_without_partial_change(
    tmp_path: Path,
) -> None:
    original = b"D" * 1_048_576
    original_sha = hashlib.sha256(original).hexdigest()
    environment, paths = _fgis_environment(
        tmp_path,
        total_size=7_227_679,
        partial=original,
        responses=[
            {"status": 200, "body_size": 200_000},
            {"status": 200, "body_size": 300_000},
        ],
    )

    result = _run_wrapper(environment)

    assert result.returncode == 1
    assert hashlib.sha256(paths["partial"].read_bytes()).hexdigest() == original_sha
    assert "no validated progress (2/2)" in paths["log"].read_text(encoding="utf-8")
    assert paths["stable"].read_bytes() == b"existing-stable-must-not-change"
    _assert_no_range_temporaries(paths["staging"])


def test_fgis_wrapper_promotes_only_before_offline_read_only_container_pipeline() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    promote_position = source.index('mv -f "${PARTIAL_FILE}" "${FINAL_FILE}"')
    pipeline_position = source.index("docker run --rm --network none")
    assert promote_position < pipeline_position
    assert 'PARTIAL_FILE="${STAGING_DIR}/${SOURCE_FILE}.downloading"' in source
    assert '"${PARTIAL_FILE}.rejected.${RUN_ID}"' in source
    assert "staged_size <= CONTENT_LENGTH_BEFORE" in source
    assert 'reject_partial "integrity"' in source
    assert 'reject_partial "metadata-change"' in source
    assert 'type=bind,src=${STAGING_DIR},dst=/source,readonly' in source
    assert 'type=bind,src=${RUNTIME_ROOT}/01_data,dst=/runtime/01_data' in source
    assert "--source-file" in source
    assert "--source-metadata-file" in source
    assert 'exit "${pipeline_exit}"' in source
    assert "pipeline exit status could not be recorded" in source


def test_fgis_wrapper_uses_runtime_release_identity_without_git_or_secret_governance() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    lowered = source.lower()

    assert "printenv MARKET_DATA_GIT_HEAD" in source
    assert "--env \"MARKET_DATA_GIT_HEAD=${MARKET_DATA_GIT_HEAD}\"" in source
    assert "RELEASE.json" not in source
    assert "git -C" not in source
    assert "git rev-parse" not in source
    assert "api_key" not in lowered
    assert "secret" not in lowered
    assert "apt-get" not in lowered
    assert "apt install" not in lowered
    assert "yum " not in lowered
    assert "dnf " not in lowered
    assert "proxy" not in lowered
    assert "crontab" not in lowered
    assert "docker build" not in lowered
