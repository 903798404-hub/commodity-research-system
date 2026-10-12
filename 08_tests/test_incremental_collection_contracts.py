from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from jsonschema import Draft202012Validator

from agri_research_agent.automation import production_data_delta as delta
from agri_research_agent.pipelines.public_data_delivery import build_production_package, validate_production_package
from pipelines.test_public_data_delivery import _current, _domestic_spread
from test_production_data_delta_activation import load_module, fas_delta

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


@pytest.mark.parametrize("bad", [None, "unpaired", "future", "extra"])
def test_fas_incremental_scope_is_closed_and_binds_reconciliation_timestamp(bad):
    host = load_module(); document = fas_delta(host)
    scope = document["domain_metadata"]["fetch_scope"]
    scope.update(mode="incremental", reconciliation_at="2026-09-01T00:00:00Z")
    if bad == "unpaired": del scope["mode"]
    elif bad == "future": scope["reconciliation_at"] = "2027-01-01T00:00:00Z"
    elif bad == "extra": scope["unchecked_history"] = True
    if bad:
        with pytest.raises(ValueError): host.validate_delta_document(document)
    else:
        assert host.validate_delta_document(document) == document
        schema = json.loads((ROOT / "09_deploy/production_data_delivery/delta_contract.schema.json").read_bytes())
        Draft202012Validator(schema).validate(document)


def test_public_worker_adopts_actual_current_including_independently_backfilled_sugar(tmp_path, monkeypatch):
    collector = load("incremental_worker", "09_deploy/scheduled_server/collect.py")
    baseline = tmp_path / "baseline"; baseline.mkdir()
    source = _domestic_spread(tmp_path / "source/historical_spread_database.parquet", value=10.0, updated_at="formal")
    formal_rows = pq.read_table(source).to_pylist(); sugar = dict(formal_rows[0])
    sugar.update(spread_group="sugar", spread_name="SR9-1", leg1_instrument="SR", leg2_instrument="SR")
    pq.write_table(pa.Table.from_pylist(formal_rows + [sugar]), source)
    _current(baseline / "public", "tankan", "r1", "same")
    formal = build_production_package(public_current_root=baseline / "public", packages_root=baseline / "packages",
        source_max_dates={}, delivery_artifacts={"domestic-spread": source})
    # Producer continuation predates the separately published sugar history.
    initial = pd.DataFrame(formal_rows)
    data = baseline / "01_data"; data.mkdir()
    initial.to_parquet(data / "historical_spread_database.parquet", index=False)
    with pd.ExcelWriter(data / "historical_spread_database.xlsx") as writer: initial.to_excel(writer, sheet_name="spread_long", index=False)
    with pd.ExcelWriter(data / "historical_price_long.xlsx") as writer: initial[["date"]].to_excel(writer, sheet_name="price_long", index=False)
    files = {p.relative_to(baseline).as_posix(): delta._identity(p) for p in data.iterdir()}
    old_bytes = {p: p.read_bytes() for p in baseline.rglob("*") if p.is_file()}
    fixture_source = tmp_path / "worker-source"; fixture_source.mkdir()
    for directory in ("02_configs", "03_src", "04_scripts"): (fixture_source / directory).mkdir()
    public_code = fixture_source / "09_deploy/scheduled_server/public.py"
    public_code.parent.mkdir(parents=True)
    public_code.write_bytes((ROOT / "09_deploy/scheduled_server/public.py").read_bytes())
    monkeypatch.setattr(collector, "SOURCE", fixture_source)
    output = tmp_path / "output"; output.mkdir()
    def update(command, **kwargs):
        project = output / "project"
        actual = pd.read_parquet(project / "01_data/historical_spread_database.parquet")
        assert set(actual.spread_name) == {"M9-1", "SR9-1"}
        (project / "01_data/update_status.json").write_text(json.dumps(dict(status="NO_CHANGE",
            target_date_data_completeness="COMPLETE", latest_date="2026-08-21", required_contracts=1, success_contracts=1)))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(collector.subprocess, "run", update)
    request = dict(job="akshare", business_date="2026-08-21", baseline_files=files,
        public_package_relative=formal.directory.relative_to(baseline).as_posix(), public_manifest=formal.manifest,
        partitioned_public_packages=True)
    result = collector.prepare(request, baseline, output, {})
    candidate = validate_production_package(output / result["package_path"])
    actual = pd.read_parquet(candidate.directory / "data" / candidate.manifest["delivery_artifacts"]["domestic-spread"]["package_path"])
    assert len(actual) == result["metrics"]["historical_rows_preserved"] == 2
    assert set(actual.spread_name) == {"M9-1", "SR9-1"}
    assert candidate.manifest["schema_version"].endswith("/3")
    for path, raw in old_bytes.items(): assert path.read_bytes() == raw
