from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from agri_research_agent.import_profit import load_soybean_config
from agri_research_agent.import_profit.runtime_store import (
    BUSINESS_KEYS_FILENAME,
    MANIFEST_FILENAME,
    QUALITY_FILENAME,
    RESULTS_FILENAME,
    SNAPSHOTS_FILENAME,
    RUNTIME_SCHEMA_VERSION,
    RuntimeReleaseIndex,
    RuntimeReleaseValidationError,
    RuntimeWriteError,
    file_sha256,
    load_runtime_release_dataset,
    resolve_current_runtime_release,
    validate_release_id,
    write_release_index_atomically,
)
from agri_research_agent.pipelines.import_profit_runtime import (
    bootstrap_import_profit_runtime,
)
from test_import_profit_components import configured_rows
from test_import_profit_query import write_dataset


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "02_configs" / "import_profit_soybean.yaml"
CONFIG = load_soybean_config(CONFIG_PATH)
NOW = datetime(2026, 7, 30, 1, 2, 3, tzinfo=timezone.utc)


def make_historical_candidate(
    tmp_path: Path,
    records=None,
) -> Path:
    candidate = tmp_path / "candidate"
    records = records or [
        configured_rows(
            date(2026, 6, 10),
            "brazil",
            2026,
            12,
            cnf=None,
        ),
        configured_rows(
            date(2026, 6, 10),
            "us_gulf",
            2026,
            12,
            cnf=125.0,
        ),
    ]
    paths = write_dataset(candidate, list(records))
    quality_path = candidate / QUALITY_FILENAME
    quality_path.write_text(
        json.dumps({"final_status": "passed_with_incomplete"}),
        encoding="utf-8",
    )
    outputs = {}
    for path in (*paths, quality_path):
        outputs[path.name] = {
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }
    manifest = {
        "schema_version": "1",
        "calculated_at": "2026-07-30T00:00:00Z",
        "output_files": outputs,
    }
    (candidate / MANIFEST_FILENAME).write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return candidate


def bootstrap_fixture(
    tmp_path: Path,
    *,
    release_id: str = "base-001",
    records=None,
):
    candidate = make_historical_candidate(tmp_path, records)
    runtime_root = tmp_path / "runtime"
    result = bootstrap_import_profit_runtime(
        candidate,
        runtime_root,
        config=CONFIG,
        config_path=CONFIG_PATH,
        release_id=release_id,
        generated_at=NOW,
    )
    return runtime_root, candidate, result


def test_bootstrap_empty_root_and_resolve_fixed_release_contract(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    candidate = make_historical_candidate(tmp_path)

    result = bootstrap_import_profit_runtime(
        candidate,
        runtime_root,
        config=CONFIG,
        config_path=CONFIG_PATH,
        release_id="base-001",
        generated_at=NOW,
    )
    loaded = load_runtime_release_dataset(runtime_root)
    resolved = loaded.resolved

    assert result.status == "success"
    assert result.promotion.generation == resolved.generation == 1
    assert resolved.previous_release_id is None
    assert resolved.manual_cnf_exists is False
    assert resolved.identity.manual_cnf_sha256 is None
    assert resolved.manifest["manual_cnf_record_count"] == 0
    assert {
        item.filename for item in resolved.files
    } == {
        BUSINESS_KEYS_FILENAME,
        SNAPSHOTS_FILENAME,
        RESULTS_FILENAME,
        QUALITY_FILENAME,
    }
    assert loaded.dataset.business_key_count == 2
    assert tuple(json.loads(
        (runtime_root / "release_index.json").read_text(encoding="utf-8")
    )) == (
        "schema_version",
        "generation",
        "current_release_id",
        "previous_release_id",
        "updated_at",
        "index_reason",
        "current_manifest_sha256",
    )
    assert not any(
        Path(value).is_absolute()
        for value in (
            resolved.runtime_root_name,
            *(
                item.filename for item in resolved.files
            ),
        )
    )


@pytest.mark.parametrize("preexisting", ["file.txt", "release_index.json"])
def test_bootstrap_rejects_nonempty_root(tmp_path, preexisting):
    candidate = make_historical_candidate(tmp_path)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / preexisting).write_text("occupied", encoding="utf-8")

    with pytest.raises(Exception, match="empty"):
        bootstrap_import_profit_runtime(
            candidate,
            runtime_root,
            config=CONFIG,
            config_path=CONFIG_PATH,
            release_id="base-001",
            generated_at=NOW,
        )


@pytest.mark.parametrize(
    "release_id", ["", ".", "..", "../bad", r"bad\path", "含中文"]
)
def test_release_id_is_strict_ascii(release_id):
    with pytest.raises(RuntimeReleaseValidationError):
        validate_release_id(release_id)


def test_expected_release_and_index_are_concurrency_guards(tmp_path):
    runtime_root, _, result = bootstrap_fixture(tmp_path)
    with pytest.raises(Exception, match="no longer Current"):
        load_runtime_release_dataset(
            runtime_root, expected_release_id="other"
        )
    with pytest.raises(Exception, match="stale"):
        load_runtime_release_dataset(
            runtime_root, expected_index_sha256="0" * 64
        )
    assert (
        load_runtime_release_dataset(
            runtime_root,
            expected_release_id="base-001",
            expected_index_sha256=result.promotion.index_sha256,
        ).resolved.release_id
        == "base-001"
    )


@pytest.mark.parametrize(
    ("target", "contents", "message"),
    [
        ("release_index.json", b"{", "JSON"),
        ("manifest.json", b"{", "Manifest"),
        (
            BUSINESS_KEYS_FILENAME,
            b"not parquet",
            "identity",
        ),
    ],
)
def test_corruption_is_rejected_without_previous_fallback(
    tmp_path, target, contents, message
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    resolved = resolve_current_runtime_release(runtime_root)
    path = (
        runtime_root / target
        if target == "release_index.json"
        else resolved.release_dir / target
    )
    path.write_bytes(contents)

    with pytest.raises(RuntimeReleaseValidationError, match=message):
        resolve_current_runtime_release(runtime_root)


def test_missing_current_directory_is_rejected(tmp_path):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    release = runtime_root / "releases" / "base-001"
    moved = runtime_root / "releases" / "not-current"
    release.rename(moved)
    with pytest.raises(RuntimeReleaseValidationError, match="does not exist"):
        resolve_current_runtime_release(runtime_root)


def test_atomic_index_replace_failure_preserves_current(tmp_path, monkeypatch):
    runtime_root, _, result = bootstrap_fixture(tmp_path)
    index_path = runtime_root / "release_index.json"
    before = index_path.read_bytes()
    current = resolve_current_runtime_release(runtime_root)
    next_index = RuntimeReleaseIndex(
        schema_version=RUNTIME_SCHEMA_VERSION,
        generation=2,
        current_release_id="next-002",
        previous_release_id="base-001",
        updated_at=NOW,
        index_reason="test",
        current_manifest_sha256=current.identity.manifest_sha256,
    )

    def fail_replace(source, target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(
        "agri_research_agent.import_profit.runtime_store.os.replace",
        fail_replace,
    )
    with pytest.raises(RuntimeWriteError, match="atomic"):
        write_release_index_atomically(
            runtime_root,
            next_index,
            expected_previous_sha256=result.promotion.index_sha256,
        )
    assert index_path.read_bytes() == before
    assert not list(runtime_root.glob(".release_index.json.*"))


def test_manifest_identity_and_key_set_corruption_are_rejected(tmp_path):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    resolved = resolve_current_runtime_release(runtime_root)
    manifest = json.loads(resolved.manifest_path.read_text(encoding="utf-8"))
    manifest["record_count"] = 999
    resolved.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    index = json.loads(
        (runtime_root / "release_index.json").read_text(encoding="utf-8")
    )
    index["current_manifest_sha256"] = file_sha256(resolved.manifest_path)
    (runtime_root / "release_index.json").write_text(
        json.dumps(index, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeReleaseValidationError, match="counts|statistics"
    ):
        load_runtime_release_dataset(runtime_root)
