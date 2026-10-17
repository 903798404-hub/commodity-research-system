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
    spreads = load("incremental_spread_worker", "09_deploy/scheduled_server/spreads.py")
    server = load("incremental_spread_calculator", "04_scripts/server_update_spreads.py")
    monkeypatch.setattr(collector, "load", lambda *a: spreads)
    original_load = spreads.load
    monkeypatch.setattr(spreads, "load", lambda name, relative:
        server if relative == "04_scripts/server_update_spreads.py" else original_load(name, relative))
    # Collection/replay is tested independently. This test isolates rebasing on
    # actual Current before calculation, including separately published sugar.
    added = pd.DataFrame({"date": [pd.Timestamp("2026-08-24")]})
    monkeypatch.setattr(spreads, "replay", lambda *a: (pd.concat([initial[["date"]], added]), added))
    output = tmp_path / "output"; output.mkdir()
    def update(**kwargs):
        project = output / "project"
        actual = pd.read_parquet(project / "01_data/historical_spread_database.parquet")
        assert set(actual.spread_name) == {"M9-1", "SR9-1"}
        extra = actual.iloc[[0]].copy(); extra["date"] = pd.Timestamp("2026-08-24")
        server.write_incremental_spread_outputs(kwargs["spread_workbook"], kwargs["spread_output"], pd.concat([actual, extra]))
        key = ("2026-08-24", str(extra.iloc[0].spread_name), str(extra.iloc[0].season))
        return {"calculated_spread_rows": 1}, (), frozenset({key})
    monkeypatch.setattr(server, "apply_direct_incremental_materialization", update)
    request = dict(job="akshare", business_date="2026-08-24", baseline_files=files,
        public_package_relative=formal.directory.relative_to(baseline).as_posix(), public_manifest=formal.manifest,
        public_preflight={"required_contracts": ["M2609"], "present_contracts": ["M2609"], "metrics": {}},
        partitioned_public_packages=True)
    result = collector.prepare(request, baseline, output, {})
    candidate = validate_production_package(output / result["package_path"])
    actual = pd.read_parquet(candidate.directory / "data" / candidate.manifest["delivery_artifacts"]["domestic-spread"]["package_path"])
    assert len(actual) == 3 and result["metrics"]["historical_rows_preserved"] == 2
    assert set(actual.spread_name) == {"M9-1", "SR9-1"}
    retained = actual.loc[pd.to_datetime(actual.date).eq("2026-08-21")].sort_values("spread_name").reset_index(drop=True)
    original = pd.read_parquet(source).sort_values("spread_name").reset_index(drop=True)
    pd.testing.assert_frame_equal(retained[original.columns], original, check_dtype=False)
    assert retained[["leg1_contract", "leg2_contract"]].isna().all().all()
    assert candidate.manifest["schema_version"].endswith("/3")
    for path, raw in old_bytes.items(): assert path.read_bytes() == raw
