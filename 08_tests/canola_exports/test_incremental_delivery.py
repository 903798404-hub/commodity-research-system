from copy import deepcopy
import base64
import gzip
import importlib.util
import json
from pathlib import Path

import pytest

from agri_research_agent.canola_exports import data, delivery, increment
from agri_research_agent.automation import production_data_delta_exports as producer
from test_delivery_exports import evidence, write
from test_exports import bundle, source

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mode", ["initialize", "no_change", "new_week", "revision"])
def test_increment_replays_exact_source_and_retains_old_years(mode):
    before = bundle(("2025-2026", source("2025-2026")), ("2026-2027", source()))
    old_evidence = {"schema_version": delivery.EVIDENCE_SCHEMA,
                    "sources": evidence("2025-2026")["sources"] + evidence()["sources"]}
    candidate, sources = deepcopy(before), deepcopy(old_evidence)
    if mode in {"new_week", "revision"}:
        raw = source(weeks=(1, 2, 3, 4, 5, 6)) if mode == "new_week" else source(weekly="2.0")
        candidate = bundle(("2025-2026", source("2025-2026")), ("2026-2027", raw))
        sources["sources"][-1] = evidence(raw=raw)["sources"][0]
        if mode == "revision":
            candidate["revisions"] = [{"crop_year": "2026-2027", "grain_week": row["grain_week"],
                "metric": "weekly_mt", "old_value": before["records"][i + 5]["weekly_mt"],
                "new_value": row["weekly_mt"], "source_sha256": row["source_sha256"],
                "observed_at": candidate["generated_at"]}
                for i, row in enumerate(candidate["records"][5:])]
    baseline = None if mode == "initialize" else before
    prior = None if mode == "initialize" else old_evidence
    update, objects = increment.build(candidate, baseline, sources, prior)
    result, result_sources = increment.resolve(update, baseline, objects, prior)
    assert result == candidate and result_sources == sources
    facts = delivery.observations(result, baseline, result_sources)
    assert facts["business_changed"] == (mode != "no_change")
    if mode == "no_change":
        assert not update["updates"] and not update["revisions"] and not objects["sources"]
        assert len(increment.canonical(update)) + len(increment.canonical(objects)) < 2000
    elif mode != "initialize":
        assert {row["crop_year"] for row in update["updates"]} == {"2026-2027"}
        assert len(objects["sources"]) == 1
        assert result["records"][:5] == before["records"][:5]


@pytest.mark.parametrize("mutation", ["baseline", "evidence_baseline", "duplicate_rows", "duplicate_reference",
    "missing_reference", "extra_object", "extra_field", "duplicate_object", "rewritten_history", "drop_week"])
def test_tampered_transport_cannot_replace_history_or_source_proof(mutation):
    before = bundle(("2026-2027", source()))
    prior = evidence()
    update, objects = increment.build(deepcopy(before), before, prior, prior)
    if mutation == "baseline": update["baseline_sha256"] = "0" * 64
    elif mutation == "evidence_baseline": objects["baseline_sha256"] = "0" * 64
    elif mutation == "duplicate_rows": update["updates"] = [before["records"][0]] * 2
    elif mutation == "duplicate_reference": objects["references"] *= 2
    elif mutation == "missing_reference": objects["references"][0][2] = "0" * 64
    elif mutation == "extra_object": objects["sources"] = evidence("2025-2026")["sources"]
    elif mutation == "extra_field": update["delete"] = []
    elif mutation == "duplicate_object": objects["sources"] = prior["sources"] * 2
    elif mutation == "rewritten_history":
        candidate = deepcopy(before); candidate["revisions"] = [{"invented": True}]
        changed_baseline = deepcopy(before); changed_baseline["revisions"] = [{"old": True}]
        with pytest.raises(ValueError, match="revision history"):
            increment.build(candidate, changed_baseline, prior, prior)
        return
    else:
        candidate = deepcopy(before); candidate["records"].pop()
        with pytest.raises(ValueError, match="drops published"):
            increment.build(candidate, before, prior, prior)
        return
    with pytest.raises(ValueError):
        increment.resolve(update, before, objects, prior)


def test_retained_sources_are_replayed_without_writing_any_expanded_copy(tmp_path):
    sample = bundle(("2025-2026", source("2025-2026")), ("2026-2027", source()))
    prior = {"schema_version": delivery.EVIDENCE_SCHEMA,
             "sources": evidence("2025-2026")["sources"] + evidence()["sources"]}
    assert delivery.archive_evidence(sample, tmp_path, retained=prior) == prior
    assert not list(tmp_path.iterdir())
    bad = deepcopy(prior); bad["sources"][0]["bytes_base64"] = base64.b64encode(b"corrupt").decode()
    with pytest.raises(ValueError): delivery.archive_evidence(sample, tmp_path, retained=bad)
    raw_path = tmp_path / f"raw/canola_exports/2026-2027/{sample['sources']['2026-2027']['sha256']}.csv"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"corrupt")
    with pytest.raises(ValueError): delivery.archive_evidence(sample, tmp_path, retained=prior)


def test_identical_official_bytes_reuse_original_evidence_across_gzip_versions(tmp_path):
    sample, prior = bundle(("2026-2027", source())), evidence()
    encoded = deepcopy(prior)
    encoded["sources"][0]["bytes_base64"] = base64.b64encode(gzip.compress(source(), mtime=42)).decode()
    assert encoded != prior and delivery.decode_evidence(encoded) == delivery.decode_evidence(prior)
    delta, objects = increment.build(sample, sample, encoded, prior)
    assert not objects["sources"]
    result, reused = increment.resolve(delta, sample, objects, prior)
    assert reused == prior and not delivery.observations(result, sample, reused)["business_changed"]
    raw_path = tmp_path / f"raw/canola_exports/2026-2027/{sample['sources']['2026-2027']['sha256']}.csv"
    raw_path.parent.mkdir(parents=True); raw_path.write_bytes(source())
    assert delivery.archive_evidence(sample, tmp_path, retained=prior) == prior
    raw_path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="byte identity"):
        delivery.archive_evidence(sample, tmp_path, retained=prior)


def test_owned_collector_does_not_expand_old_annual_inputs(tmp_path, monkeypatch):
    sample = bundle(("2025-2026", source("2025-2026")), ("2026-2027", source()))
    prior = {"schema_version": delivery.EVIDENCE_SCHEMA,
             "sources": evidence("2025-2026")["sources"] + evidence()["sources"]}
    work = tmp_path / "collection"; work.mkdir()
    def provider(argv, **kwargs):
        runtime = Path(argv[argv.index("--runtime-root") + 1])
        assert not (runtime / "raw").exists()
        assert data.load_bundle(runtime / data.STABLE) == sample
        candidate = write(runtime / "candidates/canola_exports/test/weekly.json", sample)
        return type("Result", (), {"stdout": json.dumps({"status": "PASS", "path": str(candidate)})})()
    monkeypatch.setattr(producer.delivery, "_run", provider)
    path, result = producer._collect({"python": "unused", "initial_years": 2}, work, sample, prior)
    assert result == prior and data.load_bundle(path) == sample
    assert not (work / "01_data/raw").exists()


def test_cgc_baseline_only_persists_mountpoints_with_exact_readonly_mounts(tmp_path):
    spec = importlib.util.spec_from_file_location("cgc_increment_runner", ROOT / "09_deploy/scheduled_server/run.py")
    runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    host = runner.helpers()[1]
    contents = {host.EXPORTS_STABLE: b"week", host.EXPORTS_SOURCES: b"x" * 10000, host.EXPORTS_STATUS: b"{}"}
    files = {name: {"sha256": host.sha256_bytes(raw), "size_bytes": len(raw)} for name, raw in contents.items()}
    snapshot = {"files": files, "contents": {name: base64.b64encode(raw).decode() for name, raw in contents.items()}}
    request = {"job": "canola_exports", "baseline_files": files}
    runner.copy_snapshot(host, snapshot, tmp_path / "baseline", request)
    assert (tmp_path / "baseline" / host.EXPORTS_STABLE).stat().st_size == 0
    assert (tmp_path / "baseline" / host.EXPORTS_SOURCES).stat().st_size == 0
    assert (tmp_path / "baseline" / host.EXPORTS_STATUS).read_bytes() == b"{}"
    config = {"job": "canola_exports", "image_id": "sha256:" + "a" * 64, "allocation_root": "/protected/allocation"}
    command = runner.collector_command(config, tmp_path)
    for name in (host.EXPORTS_STABLE, host.EXPORTS_SOURCES):
        assert f"type=bind,src=/protected/allocation/{name},dst=/baseline/{name},readonly" in command
