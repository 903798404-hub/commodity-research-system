from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "04_scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import run_local_dashboard  # noqa: E402


def _write_database(path: Path) -> None:
    pd.DataFrame(
        {
            "date": ["2026-06-19", "2026-06-20", "2026-06-20"],
            "commodity": ["豆粕", "菜粕", "豆粕"],
            "region": ["华东", "华南", "华东"],
        }
    ).to_parquet(path, index=False)


def test_imports_prints_summary_then_starts_dashboard(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    database = tmp_path / "basis_quotes.parquet"
    _write_database(database)
    monkeypatch.setattr(run_local_dashboard, "DATABASE_FILE", database)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(run_local_dashboard.subprocess, "run", fake_run)

    assert run_local_dashboard.main() == 0
    output = capsys.readouterr().out
    assert "最新日期：2026-06-20" in output
    assert "总行数：3" in output
    assert "品种数量：2" in output
    assert "地区数量：2" in output
    assert len(calls) == 2
    assert calls[0][-1].endswith("import_basis_excel.py")
    assert calls[1][-1].endswith("streamlit_app.py")


def test_does_not_start_dashboard_when_import_fails(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 2)

    monkeypatch.setattr(run_local_dashboard.subprocess, "run", fake_run)

    assert run_local_dashboard.main() == 2
    assert len(calls) == 1
    assert "面板未启动" in capsys.readouterr().err
