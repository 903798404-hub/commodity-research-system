from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from filelock import FileLock
import pytest

from agri_research_agent.import_profit.runtime_store import (
    resolve_current_runtime_release,
)
from test_import_profit_runtime_store import (
    CONFIG_PATH,
    NOW,
    bootstrap_fixture,
    make_historical_candidate,
)


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(os.environ.get("IMPORT_PROFIT_TEST_PYTHON", os.sys.executable))
BOOTSTRAP = (
    ROOT / "04_scripts" / "import_profit"
    / "bootstrap_import_profit_runtime.py"
)
UPDATE = (
    ROOT / "04_scripts" / "import_profit"
    / "update_import_profit_cnf.py"
)


def run_cli(script: Path, *arguments: str):
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [str(PYTHON), "-B", str(script), *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def write_updates(path: Path, updates) -> Path:
    path.write_text(json.dumps({"updates": updates}), encoding="utf-8")
    return path


def update_arguments(runtime_root: Path, updates_path: Path):
    current = resolve_current_runtime_release(runtime_root)
    return (
        "--runtime-root",
        str(runtime_root),
        "--config",
        str(CONFIG_PATH),
        "--expected-release-id",
        current.release_id,
        "--expected-index-sha256",
        current.identity.index_sha256,
        "--expected-manual-cnf-sha256",
        current.identity.manual_cnf_sha256 or "NONE",
        "--updates-json",
        str(updates_path),
        "--calculated-at",
        "2026-07-30T02:00:00Z",
        "--batch-id",
        "cli-batch-001",
        "--release-id",
        "cli-manual-002",
        "--lock-timeout-seconds",
        "0",
    )


@pytest.mark.parametrize("script", [BOOTSTRAP, UPDATE])
def test_cli_help(script):
    completed = run_cli(script, "--help")
    assert completed.returncode == 0
    assert "usage:" in completed.stdout.lower()


def test_bootstrap_cli_success_and_bounded_summary(tmp_path):
    candidate = make_historical_candidate(tmp_path)
    runtime_root = tmp_path / "runtime"
    completed = run_cli(
        BOOTSTRAP,
        "--historical-candidate-dir",
        str(candidate),
        "--config",
        str(CONFIG_PATH),
        "--runtime-root",
        str(runtime_root),
        "--release-id",
        "cli-base-001",
        "--generated-at",
        NOW.isoformat(),
    )
    payload = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert payload["status"] == "success"
    assert payload["generation"] == 1
    assert payload["record_count"] == 2
    assert str(tmp_path) not in completed.stdout


@pytest.mark.parametrize("cnf", [100.0, 0.0, None])
def test_update_cli_accepts_positive_zero_and_null(tmp_path, cnf):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    updates = write_updates(
        tmp_path / "updates.json",
        [
            {
                "business_date": "2026-06-10",
                "origin": "brazil",
                "shipment_period": "2026-12",
                "cnf_cents_per_bushel": cnf,
            }
        ],
    )
    completed = run_cli(UPDATE, *update_arguments(runtime_root, updates))
    payload = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert payload["status"] == "success"
    assert payload["changed_count"] == 1
    assert str(tmp_path) not in completed.stdout


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        json.dumps({"updates": [{
            "business_date": "2026-06-10",
            "origin": "brazil",
            "shipment_period": "2026-12",
            "cnf_cents_per_bushel": 100,
            "unknown": True,
        }]}).encode(),
        json.dumps({"wrong": []}).encode(),
    ],
)
def test_update_cli_rejects_invalid_or_unknown_json(tmp_path, payload):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    updates = tmp_path / "updates.json"
    updates.write_bytes(payload)
    completed = run_cli(UPDATE, *update_arguments(runtime_root, updates))
    output = json.loads(completed.stdout)

    assert completed.returncode == 2
    assert output["error"] == "RuntimeCliError"
    assert str(tmp_path) not in completed.stdout


def test_update_cli_rejects_duplicate_keys(tmp_path):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    item = {
        "business_date": "2026-06-10",
        "origin": "brazil",
        "shipment_period": "2026-12",
        "cnf_cents_per_bushel": 100,
    }
    updates = write_updates(tmp_path / "updates.json", [item, item])
    completed = run_cli(UPDATE, *update_arguments(runtime_root, updates))
    payload = json.loads(completed.stdout)

    assert completed.returncode == 2
    assert payload["error"] == "RuntimePipelineError"


def test_update_cli_lock_conflict_has_finite_bounded_output(tmp_path):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    updates = write_updates(
        tmp_path / "updates.json",
        [{
            "business_date": "2026-06-10",
            "origin": "brazil",
            "shipment_period": "2026-12",
            "cnf_cents_per_bushel": 100,
        }],
    )
    lock = FileLock(str(runtime_root / ".runtime.lock"))
    with lock.acquire(timeout=0):
        completed = run_cli(
            UPDATE, *update_arguments(runtime_root, updates)
        )
    payload = json.loads(completed.stdout)

    assert completed.returncode == 2
    assert payload == {
        "error": "RuntimeLockedError",
        "status": "locked",
    }
    assert str(tmp_path) not in completed.stdout
