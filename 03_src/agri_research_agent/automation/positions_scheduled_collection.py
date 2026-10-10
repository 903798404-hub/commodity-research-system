"""Collect only implemented official sources into a fresh external four-board store."""
from __future__ import annotations

from datetime import date, timedelta
import re

from agri_research_agent.automation import production_data_delta as delivery
from agri_research_agent.data_sources.nutstore_basis import assert_external_output
from agri_research_agent.oilseed_positions.model import config as domain_config
from agri_research_agent.oilseed_positions.sources import Sources
from agri_research_agent.positions import delivery as codec
from agri_research_agent.positions.bundle import verified_files
from agri_research_agent.sugar_positions.sources import OfficialSources
from agri_research_agent.sugar_positions.storage import publish, read_snapshot


def collect_bundle(baseline, work, *, markets, today=None):
    today = today or date.today()
    delivery.require(markets in {"domestic", "foreign"}, "scheduled positions market scope invalid")
    snapshots, _ = codec.validate_archive(baseline, delivery.ROOT)
    store = assert_external_output(work / "collection")
    codec.materialize(baseline, store)
    sugar = delivery._module(delivery.ROOT, "04_scripts/sugar_positions/update_sugar_positions.py", "scheduled_sugar")
    oilseed = delivery._module(delivery.ROOT, "04_scripts/oilseed_positions/update_positions.py", "scheduled_oilseed")
    specs, attempts = domain_config(delivery.ROOT), []
    selected = ("sugar", "rapeseed") if markets == "domestic" else ("sugar", "rapeseed", "soybean")
    for domain in selected:
        old = snapshots[domain]
        # Keep CFTC query identity and all already published years covered by that source.
        years = [int(match.group(1)) for source in old["sources"].values()
                 if (match := re.search(r"(20\d{2})-01-01T00", source["url"]))]
        start_year = min(years, default=today.year - 1)
        existing = {row["report_date"] for row in old["domestic"]}
        if domain == "sugar":
            rows = sugar.collect(OfficialSources(), markets=markets, start_year=start_year,
                start_day=today, end_day=today, existing_dates=existing)
        else:
            # No DCE, browser, Sina or pending FCPO requests in this automatic chain.
            spec = specs[domain]
            rows = oilseed.collect(Sources(), spec, markets=markets, start_year=start_year,
                start_day=today, end_day=today, euro_start=max(date(2026, 9, 30), today - timedelta(days=35)),
                existing_dates=existing)
        foreign, domestic, captures, observed = rows
        attempts.extend({"domain": domain, **item} for item in observed)
        root = store / domain
        (root / "last_attempt.json").write_bytes(delivery.canonical_json_bytes({"attempts": observed}))
        if foreign or domestic:
            publish(root, foreign, domestic, captures, observed)
    bundle = assert_external_output(work / "bundle")
    bundle.mkdir()
    manifest = {"schema_version": "commodity-positions-bundle/1", "domains": {}, "files": {}}
    for domain in codec.DOMAINS:
        snapshot, files = verified_files(store / domain, delivery.ROOT, domain)
        manifest["domains"][domain] = {"release_id": snapshot["release_id"],
            "foreign_rows": len(snapshot["foreign"]), "domestic_rows": len(snapshot["domestic"])}
        for rel, raw in files.items():
            target = bundle / "domains" / domain / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            manifest["files"][target.relative_to(bundle).as_posix()] = codec.digest(raw)
    (bundle / "bundle.json").write_bytes(codec.canonical(manifest))
    candidate = codec.archive_bundle(bundle, delivery.ROOT)
    semantic = codec.observations(candidate, baseline, delivery.ROOT)
    # Normal scheduled increments never silently approve changes to published report partitions.
    delivery.require(not semantic["revised_partitions"], "positions historical revision requires review")
    # Boards and markets outside the request must retain exact verified inputs.
    current = codec.validate_archive(candidate, delivery.ROOT)[0]
    for domain in codec.DOMAINS:
        if domain not in selected:
            delivery.require(current[domain] == snapshots[domain], "unselected positions board changed")
        else:
            untouched = "foreign" if markets == "domestic" else "domestic"
            delivery.require(current[domain][untouched] == snapshots[domain][untouched],
                             "unselected positions market changed")
    return bundle, attempts
