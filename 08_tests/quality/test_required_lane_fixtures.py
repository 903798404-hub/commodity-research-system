from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import pytest

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


def test_checkout_scaffold_creates_only_empty_numbered_directories(tmp_path):
    module = _module()
    root = tmp_path / 'checkout'
    root.mkdir()
    evidence = module.prepare_checkout(root)
    assert [r['relative_path'] for r in evidence] == list(module.NUMBERED_DIRECTORIES)
    assert all(not list((root / r['relative_path']).iterdir()) for r in evidence)
    assert all(r['business_files_created'] is False for r in evidence)
    (root / '01_data').rmdir()
    (root / '01_data').write_text('not a directory')
    with pytest.raises(ValueError, match='INPUT_PREPARATION_FAILED'):
        module.prepare_checkout(root)


def test_both_lanes_use_same_existing_preparation():
    workflow = (ROOT / '.github/workflows/trusted-main-admission.yml').read_text(encoding='utf-8')
    assert workflow.count('--prepare-checkout-directories') == 2
    full_job = workflow.split('  full:\n', 1)[1]
    assert '../executor/04_scripts/quality/required_lane_fixtures.py --source-root .' in full_job
    assert full_job.index('--prepare-checkout-directories') < full_job.index('Execute complete full regression')


def test_high_risk_browser_setup_uses_executor_interpreter_and_root_browser_cache():
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/trusted-main-admission.yml').read_text(encoding='utf-8'))
    steps = workflow['jobs']['linux']['steps']
    setup = next(step for step in steps if step.get('name') == 'Prepare real host browser for HIGH_RISK application smoke')
    execute = next(step for step in steps if step.get('name') == 'Execute same-entry HIGH_RISK success and fresh rollback with synthetic trust')
    assert steps.index(setup) < steps.index(execute)
    assert setup['if'] == execute['if']
    assert 'python_bin="$(command -v python)"' in setup['run']
    assert '"$python_bin" -I -m pip install playwright==1.55.0' in setup['run']
    assert 'sudo "$python_bin" -I -m playwright install --with-deps chromium' in setup['run']
    assert 'p.chromium.launch(headless=True)' in setup['run']
    assert 'DEPENDENCY_PREFLIGHT_NOT_APPLICATION_ACCEPTANCE' in setup['run']
    assert 'sudo RUNNER_ENVIRONMENT=github-hosted "$python_bin" -I -B' in execute['run']


def test_short_lived_docker_evidence_consumed_before_long_executor_without_interpreter_drift():
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/trusted-main-admission.yml').read_text(encoding='utf-8'))
    steps = workflow['jobs']['linux']['steps']
    named = {step.get('name'): step for step in steps if 'name' in step}
    execute = named['Execute same-entry HIGH_RISK success and fresh rollback with synthetic trust']
    current = named['Validate host release timestamps on current Python']
    legacy = named['Validate host release timestamps on actual Python 3.10.12']
    assert steps.index(current) < steps.index(legacy) < steps.index(execute)
    original = next(step for step in steps if step.get('id') == 'host-python312')
    assert original['with']['python-version'] == '3.12'
    assert "python_bin='${{ steps.host-python312.outputs.python-path }}'" in execute['run']
    assert 'command -v python' not in execute['run']
    assert current['if'] == legacy['if'] == execute['if']
    service = named['Execute required real Docker application service identity evidence']
    legacy_setup = next(step for step in steps if step.get('id') == 'host-python310')
    assert steps.index(service) < steps.index(legacy_setup) < steps.index(legacy)
    assert legacy_setup['with']['python-version'] == '3.10.12'
    for step in (current, legacy):
        assert '--docker-evidence' in step['run'] and 'host_release_timestamp_compatibility.py' in step['run']
        assert "--base '0d7b86ddafbed0e7b063ae1097d7e07ee36e9f00'" in step['run']
        assert 'needs.plan.outputs.base_commit' not in step['run']
    assert 'needs.plan.outputs.base_commit' in workflow['jobs']['full']['steps'][1]['with']['ref']


def test_fixture_identity_is_deterministic_and_missing_or_changed_input_rejected(tmp_path):
    module = _module()
    first, second = tmp_path / 'base-fixtures', tmp_path / 'candidate-fixtures'
    a, b = module.build(ROOT, first), module.build(ROOT, second)
    assert a['input_identity'] == b['input_identity']
    assert first != second and a['spread_reference_data_root'] != b['spread_reference_data_root']
    path = second / 'spread-reference/02_configs/historical_spread_config.xlsx'
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='fixture bytes changed'):
        module.fixture_input_identity(second)
    # This deliberately damaged private test fixture must permit unlink on
    # POSIX; writable file mode alone does not grant its parent directory write.
    path.parent.chmod(0o700)
    path.unlink()
    with pytest.raises(ValueError, match='required fixture file'):
        module.fixture_input_identity(second)


def test_input_equivalence_rejects_missing_setup_and_changed_dataset(tmp_path):
    import copy
    module = _module()
    a = module.build(ROOT, tmp_path / 'inputs')
    a['checkout_directories'] = [dict(relative_path=n) for n in module.NUMBERED_DIRECTORIES]
    b = copy.deepcopy(a)
    assert module.compare_prepared_inputs(a, b)['result'] == 'PASS'
    b['checkout_directories'] = []
    with pytest.raises(ValueError, match='missing checkout directory setup'):
        module.compare_prepared_inputs(a, b)
    b = copy.deepcopy(a)
    b['input_identity']['files'][0]['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='digest mismatch'):
        module.compare_prepared_inputs(a, b)
