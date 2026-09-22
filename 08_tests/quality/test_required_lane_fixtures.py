from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "04_scripts" / "quality" / "required_lane_fixtures.py"


def _module():
    spec = importlib.util.spec_from_file_location("required_lane_fixtures", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_builder_creates_minimal_read_only_roots_without_external_data(tmp_path: Path) -> None:
    output = tmp_path / "required-fixtures"

    evidence = _module().build(ROOT, output)

    spread = Path(evidence["spread_reference_data_root"])
    public = Path(evidence["public_market_data_runtime_root"])
    history = spread / "01_data" / "historical_spread_database.parquet"
    config = spread / "02_configs" / "historical_spread_config.xlsx"
    current = public / "public-market-data" / "lutou-three-oil" / "current.json"
    pointer = json.loads(current.read_text(encoding="utf-8"))
    release = current.parent / "releases" / pointer["release_id"]

    assert evidence["network_dependency"] is False
    assert evidence["production_data_dependency"] is False
    assert history.is_file() and config.is_file()
    assert (release / "manifest.json").is_file()
    assert (release / "observations.parquet").is_file()
    assert pd.read_parquet(history).shape == (72, 16)
    assert pd.read_parquet(release / "observations.parquet").shape[0] == 240
    if os.name != "nt":
        for path in output.rglob("*"):
            if path.is_file():
                assert not path.stat().st_mode & stat.S_IWUSR


def test_workflow_provisions_fixtures_before_required_and_full_tests() -> None:
    workflow = (ROOT / ".github" / "workflows" / "trusted-main-admission.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.count("Prepare deterministic required fixtures") == 2
    assert (
        "SPREAD_REFERENCE_DATA_ROOT=$RUNNER_TEMP/required-fixtures/spread-reference"
        in workflow
    )
    assert (
        "PUBLIC_MARKET_DATA_RUNTIME_ROOT=$RUNNER_TEMP/required-fixtures/public-runtime"
        in workflow
    )
    assert workflow.count('>> "$GITHUB_ENV"') == 4
    assert workflow.index("Prepare deterministic required fixtures") < workflow.index(
        "Execute required linux tests"
    )
    script = SCRIPT.read_text(encoding="utf-8")
    assert "curl " not in script
    assert "http://" not in script
    assert "https://" not in script


def test_final_aggregation_uses_only_its_runtime_dependency() -> None:
    workflow = (ROOT / ".github" / "workflows" / "trusted-main-admission.yml").read_text(
        encoding="utf-8"
    )
    final_job = workflow.split("  final:\n", 1)[1].split("  full:\n", 1)[0]

    assert (
        "python -I -m pip install PyYAML==6.0.3 jsonschema==4.26.0" in final_job
    )
    assert "requirements-dev.in" not in final_job
    assert "Compare exact base and candidate full regression" in final_job
    assert "Aggregate actual required platform jobs" in final_job
