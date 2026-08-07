from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "04_scripts" / "import_profit"
CONFIG = ROOT / "02_configs" / "import_profit_soybean.yaml"


def load_script(name: str, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "name",
    [
        "refresh_import_profit_external_inputs",
        "capture_import_profit_night_session_close",
        "materialize_import_profit_daily_release",
    ],
)
def test_three_cli_help(name):
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / f"{name}.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout


def materialized(status="waiting_for_dce_capture"):
    return SimpleNamespace(
        status=status,
        business_date=date(2026, 8, 5),
        release_id="base-001",
        generation=1,
        previous_release_id=None,
        index_sha256="A" * 64,
        appended_business_key_count=0,
        success_count_delta=0,
        incomplete_count_delta=0,
        external_input_candidate_id="external-001",
        dce_candidate_id=None,
        message="waiting",
        total_seconds=0.01,
    )


def common_args(tmp_path):
    return [
        "--runtime-root",
        str(tmp_path / "runtime"),
        "--external-input-root",
        str(tmp_path / "external"),
        "--dce-input-root",
        str(tmp_path / "dce"),
        "--config",
        str(CONFIG),
        "--business-date",
        "2026-08-05",
        "--expected-runtime-release-id",
        "base-001",
        "--expected-runtime-index-sha256",
        "A" * 64,
        "--release-id",
        "daily-001",
        "--batch-id",
        "batch-001",
        "--calculated-at",
        "2026-08-05T02:00:00Z",
    ]


def test_materialize_cli_prints_bounded_path_free_summary(
    tmp_path, monkeypatch, capsys
):
    module = load_script("materialize_import_profit_daily_release", monkeypatch)
    monkeypatch.setattr(module, "run_materialization", lambda args: materialized())

    assert module.main(common_args(tmp_path)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "waiting_for_dce_capture"
    assert str(tmp_path) not in json.dumps(payload)


def test_dce_cli_calls_capture_then_common_materializer_without_sql(
    tmp_path, monkeypatch, capsys
):
    module = load_script("capture_import_profit_night_session_close", monkeypatch)
    capture = SimpleNamespace(
        outcome=SimpleNamespace(
            candidate_id="dce-001",
            attempt_status="success",
            requested_contracts=("M2701", "Y2701"),
            available_contracts=("M2701", "Y2701"),
            missing_contracts=(),
        )
    )
    called = []
    monkeypatch.setattr(
        module,
        "capture_and_store_dce_night_session_close",
        lambda *args, **kwargs: called.append("capture") or capture,
    )
    monkeypatch.setattr(
        module,
        "try_materialize_import_profit_business_day",
        lambda *args, **kwargs: called.append("materialize")
        or materialized("materialized"),
    )
    args = common_args(tmp_path) + [
        "--candidate-id",
        "dce-001",
        "--snapshot-batch-id",
        "snapshot-001",
    ]

    assert module.main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert called == ["capture", "materialize"]
    assert payload["dce_capture_status"] == "success"
    assert "sql" not in json.dumps(payload).lower()


def test_external_cli_converts_existing_file_then_calls_common_materializer(
    tmp_path, monkeypatch, capsys
):
    module = load_script("refresh_import_profit_external_inputs", monkeypatch)
    source = tmp_path / "prices.sql"
    source.write_text("read-only SQL dump", encoding="utf-8")
    called = []

    def fake_build(sql, output, **kwargs):
        called.append("reuters")
        Path(output).mkdir()

    external = SimpleNamespace(
        status="promoted",
        candidate=SimpleNamespace(
            candidate_id="external-001", candidate_status="passed"
        ),
    )
    monkeypatch.setattr(module, "build_reuters_candidate", fake_build)
    monkeypatch.setattr(
        module,
        "store_morning_external_inputs_candidate",
        lambda *args, **kwargs: called.append("external") or external,
    )
    monkeypatch.setattr(
        module,
        "try_materialize_import_profit_business_day",
        lambda *args, **kwargs: called.append("materialize") or materialized(),
    )
    args = common_args(tmp_path) + [
        "--sql-path",
        str(source),
        "--working-dir",
        str(tmp_path / "working"),
        "--source-file-uploaded-at",
        "2026-08-05T00:35:00Z",
        "--prepared-at",
        "2026-08-05T00:40:00Z",
    ]

    assert module.main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert called == ["reuters", "external", "materialize"]
    assert payload["source_sql_filename"] == "prices.sql"
    assert str(tmp_path) not in json.dumps(payload)
    assert source.read_text("utf-8") == "read-only SQL dump"
