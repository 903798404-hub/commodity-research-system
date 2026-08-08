from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[2]
WRAPPER = (
    REPOSITORY
    / "09_deploy/soybean_exports/run_fgis_uploaded_source_update.sh"
)
RUNTIME_GIT_HEAD = "c07f2635a86a1c4f3e13167a982e5bb56e391b91"
IMAGE_ID = "sha256:" + "b" * 64


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
    raise AssertionError("Bash is required to validate the production wrapper")


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


def _source_bytes() -> bytes:
    return (
        "Thursday,Cert Date,Grain,Destination,Metric Ton\n"
        "20260730,20260730,SOYBEANS,CHINA,10\n"
    ).encode()


def _write_upload(inbox: Path, *, source_sha256: str | None = None) -> str:
    inbox.mkdir(parents=True, exist_ok=True)
    content = _source_bytes()
    digest = hashlib.sha256(content).hexdigest()
    source = inbox / "CY2026.csv.uploading"
    source.write_bytes(content)
    observed = {
        "content_length": len(content),
        "last_modified": "Mon, 03 Aug 2026 15:00:26 GMT",
        "etag": '"reference"',
        "accept_ranges": "bytes",
    }
    metadata = {
        "schema_version": 1,
        "source_authority": "USDA FGIS",
        "source_channel": "yearly_export_grain_csv",
        "calendar_year": 2026,
        "source_file": "CY2026.csv",
        "official_url": (
            "https://fgisonline.ams.usda.gov/exportgrainreport/CY2026.csv"
        ),
        "source_sha256": source_sha256 or digest,
        "source_size": len(content),
        "content_length": len(content),
        "accept_ranges": "bytes",
        "last_modified": observed["last_modified"],
        "etag": observed["etag"],
        "download_completed_at_utc": "2026-08-08T01:02:03Z",
        "metadata_before": observed,
        "metadata_after": observed,
    }
    (inbox / "CY2026.csv.metadata.json.uploading").write_text(
        json.dumps(metadata), encoding="utf-8", newline="\n"
    )
    return digest


def _environment(
    tmp_path: Path,
    *,
    flock_exit: int = 0,
    docker_exit: int = 0,
) -> tuple[dict[str, str], Path, Path, Path]:
    mock_bin = tmp_path / "mock-bin"
    records = tmp_path / "records"
    runtime = tmp_path / "runtime"
    source_root = tmp_path / "fgis-yearly"
    logs = tmp_path / "logs"
    mock_bin.mkdir()
    records.mkdir()
    (runtime / "01_data").mkdir(parents=True)
    _write_executable(
        mock_bin / "flock",
        "#!/usr/bin/env bash\nexit \"${MOCK_FLOCK_EXIT}\"\n",
    )
    _write_executable(
        mock_bin / "python3",
        f'#!/usr/bin/env bash\nexec "{_bash_path(Path(sys.executable))}" "$@"\n',
    )
    _write_executable(
        mock_bin / "docker",
        """#!/usr/bin/env bash
printf '%s\n' "$1" >>"${MOCK_DOCKER_CALLS}"
if [[ "$1" == "inspect" ]]; then
    if [[ "$*" == *".State.Running"* ]]; then
        printf 'true\n'
    elif [[ "$*" == *".Image"* ]]; then
        printf '%s\n' "${MOCK_IMAGE_ID}"
    else
        exit 2
    fi
    exit 0
fi
if [[ "$1" == "exec" ]]; then
    printf '%s\n' "${MOCK_RUNTIME_GIT_HEAD}"
    exit 0
fi
if [[ "$1" == "image" && "${2-}" == "inspect" ]]; then
    printf '%s\n' "${MOCK_RUNTIME_GIT_HEAD}"
    exit 0
fi
if [[ "$1" == "run" ]]; then
    printf '%s\n' "$@" >"${MOCK_DOCKER_ARGS}"
    exit "${MOCK_DOCKER_EXIT}"
fi
exit 2
""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "MARKET_DATA_WRAPPER_PATH": f"{_bash_path(mock_bin)}:/usr/bin:/bin",
            "MARKET_DATA_FGIS_LOCK_FILE": _bash_path(tmp_path / "fgis.lock"),
            "MARKET_DATA_FGIS_RUNTIME_HOST_ROOT": _bash_path(runtime),
            "MARKET_DATA_FGIS_SOURCE_ROOT": _bash_path(source_root),
            "MARKET_DATA_FGIS_UPLOAD_INBOX": _bash_path(source_root / "inbox"),
            "MARKET_DATA_FGIS_ACCEPTED_ROOT": _bash_path(
                source_root / "manual-sources"
            ),
            "MARKET_DATA_FGIS_LOG_ROOT": _bash_path(logs),
            "MARKET_DATA_FGIS_CALENDAR_YEAR": "2026",
            "MOCK_FLOCK_EXIT": str(flock_exit),
            "MOCK_DOCKER_EXIT": str(docker_exit),
            "MOCK_DOCKER_CALLS": _bash_path(records / "docker-calls.txt"),
            "MOCK_DOCKER_ARGS": _bash_path(records / "docker-args.txt"),
            "MOCK_IMAGE_ID": IMAGE_ID,
            "MOCK_RUNTIME_GIT_HEAD": RUNTIME_GIT_HEAD,
        }
    )
    return environment, source_root, records, logs


def _run(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash_executable(), _bash_path(WRAPPER)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def test_uploaded_source_is_atomically_accepted_and_processed_offline(
    tmp_path: Path,
) -> None:
    environment, source_root, records, logs = _environment(tmp_path)
    digest = _write_upload(source_root / "inbox")

    result = _run(environment)

    assert result.returncode == 0, result.stderr
    release = source_root / "manual-sources" / "CY2026" / digest
    assert (release / "CY2026.csv").read_bytes() == _source_bytes()
    metadata = json.loads(
        (release / "CY2026.csv.metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["source_filename"] == "CY2026.csv"
    assert metadata["downloaded_at"] == "2026-08-08T01:02:03Z"
    assert metadata["uploaded_at"].endswith("Z")
    assert len(metadata["upload_metadata_sha256"]) == 64
    assert not (source_root / "inbox" / "CY2026.csv.uploading").exists()
    docker_args = (records / "docker-args.txt").read_text(encoding="utf-8")
    assert "--network\nnone\n" in docker_args
    assert "dst=/source,readonly" in docker_args
    assert "--source-file\n/source/CY2026.csv\n" in docker_args
    assert "--source-metadata-file\n/source/CY2026.csv.metadata.json\n" in docker_args
    assert IMAGE_ID in docker_args
    wrapper = WRAPPER.read_text(encoding="utf-8")
    assert "curl" not in wrapper
    assert "fgisonline" in wrapper
    assert "pipeline completed successfully" in (
        logs / "fgis_uploaded_source_update.log"
    ).read_text(encoding="utf-8")


def test_invalid_uploaded_sha_is_quarantined_before_docker(tmp_path: Path) -> None:
    environment, source_root, records, logs = _environment(tmp_path)
    _write_upload(source_root / "inbox", source_sha256="0" * 64)

    result = _run(environment)

    assert result.returncode == 1
    assert not (records / "docker-calls.txt").exists()
    rejected = list((source_root / "manual-sources" / "rejected").glob("failed-*"))
    assert len(rejected) == 1
    assert (rejected[0] / "CY2026.csv").is_file()
    assert "metadata validation failed" in (
        logs / "fgis_uploaded_source_update.log"
    ).read_text(encoding="utf-8")


def test_duplicate_source_is_preserved_as_evidence_and_reprocessed(
    tmp_path: Path,
) -> None:
    environment, source_root, records, _logs = _environment(tmp_path)
    digest = _write_upload(source_root / "inbox")
    assert _run(environment).returncode == 0
    _write_upload(source_root / "inbox")

    second = _run(environment)

    assert second.returncode == 0
    releases = list((source_root / "manual-sources" / "CY2026").glob(digest))
    assert len(releases) == 1
    duplicates = list((source_root / "manual-sources" / "duplicates").iterdir())
    assert len(duplicates) == 1
    assert (records / "docker-args.txt").is_file()


def test_lock_contention_skips_without_consuming_upload(tmp_path: Path) -> None:
    environment, source_root, records, logs = _environment(tmp_path, flock_exit=1)
    _write_upload(source_root / "inbox")

    result = _run(environment)

    assert result.returncode == 0
    assert (source_root / "inbox" / "CY2026.csv.uploading").is_file()
    assert not (records / "docker-calls.txt").exists()
    assert "already has a running instance" in (
        logs / "fgis_uploaded_source_update.log"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("exit_code", [9, 37])
def test_pipeline_exit_code_is_preserved(tmp_path: Path, exit_code: int) -> None:
    environment, source_root, _records, logs = _environment(
        tmp_path, docker_exit=exit_code
    )
    _write_upload(source_root / "inbox")

    result = _run(environment)

    assert result.returncode == exit_code
    digest = hashlib.sha256(_source_bytes()).hexdigest()
    assert (
        source_root / "manual-sources" / "CY2026" / digest / "CY2026.csv"
    ).is_file()
    assert not (source_root / "inbox" / "CY2026.csv.uploading").exists()
    assert f"exit code {exit_code}" in (
        logs / "fgis_uploaded_source_update.log"
    ).read_text(encoding="utf-8")
