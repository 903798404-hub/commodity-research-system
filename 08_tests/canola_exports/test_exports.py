from __future__ import annotations

import csv
import io
import json
from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import openpyxl
from filelock import FileLock, Timeout

from agri_research_agent.canola_exports import data, update
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


def source(year="2026-2027", weeks=(1, 2, 3, 4, 5), *, weekly="1.0", cumulative="10.0", old_header=False):
    fields = ["Crop Year", "Grain Week", "Week Ending Date", "worksheet", "metric", "period", "grain", "grade", "Region", "Ktonnes"]
    if old_header:
        fields = [data.HEADER.get(k, k) for k in fields]
    rows = []
    start = int(year[:4])
    for week in weeks:
        ending = (date(start, 8, 1) + timedelta(days=week * 7)).strftime("%d/%m/%Y")
        for period, value in (("Current Week", weekly), ("Crop Year", cumulative)):
            for port in sorted(data.PORTS):
                rows.append([year, week, ending, "Terminal Disposition", "Export Destinations", period, "Canola", "", port, value])
            rows.append([year, week, ending, "Primary Shipment Distribution", "Shipment Distribution", period, "Canola", "", "Export Destinations", value])
            # These are alternate breakdowns, not additional national exports.
            rows.append([year, week, ending, "Terminal Exports", "Exports", period, "Canola", "No.1 CANADA", "Vancouver", "999"])
            rows.append([year, week, ending, "Primary Shipment Distribution", "Sk-Shipment Distribution", period, "Canola", "", "Export Destinations", "999"])
    return encode(fields, rows)


def encode(fields, rows):
    handle = io.StringIO()
    writer = csv.writer(handle)
    writer.writerow(fields)
    writer.writerows(rows)
    return handle.getvalue().encode("utf-8-sig")


def bundle(*sources):
    result = {"schema_version": data.SCHEMA, "generated_at": update.now(), "sources": {}, "records": [], "revisions": []}
    for year, raw in sources:
        result["records"].extend(data.parse_csv(raw, year))
        result["sources"][year] = {"source_url": data.source_url(year), "sha256": data.digest(raw), "retrieved_at": update.now()}
    return result


def excel_report(year="2026-2027", week=3, weekly=0):
    workbook = openpyxl.Workbook()
    workbook.active.append([year])
    workbook.active.append([f"Week {week}"])
    sheet = workbook.create_sheet("Summary")
    sheet.append(["(in 000's of tonnes)"])
    sheet.append(["", "Canola"])
    sheet.append(["Exports"])
    sheet.append(["Current Week", weekly])
    sheet.append(["To Date", 70])
    sheet.append(["Domestic Disappearance"])
    handle = io.BytesIO()
    workbook.save(handle)
    workbook.close()
    return handle.getvalue()


@pytest.fixture
def context(tmp_path):
    (tmp_path / ".market-data-runtime.json").write_text(json.dumps({"schema_version": 1,
        "runtime_id": "export-test", "classification": "fixture", "module_id": data.MODULE_ID, "created_at": update.now()}))
    return RuntimeContext(RuntimeMode.FIXTURE, data.MODULE_ID, tmp_path)


@pytest.mark.parametrize("old_header", [False, True])
def test_official_columns_units_and_no_double_count(old_header):
    raw = source(old_header=old_header, cumulative="1,234.5")
    records = data.parse_csv(raw, "2026-2027")
    assert records[0]["weekly_mt"] == 7000
    assert records[0]["cumulative_mt"] == 8641500
    assert records[0]["week_ending"] == "2026-08-08"
    assert records[0]["source_sha256"] == data.digest(raw)
    assert records[-1]["cumulative_mt"] != sum(r["weekly_mt"] for r in records)


@pytest.mark.parametrize("value, expected", [("0", 0), ("", None), (".", None), ("..", None), ("-", None)])
def test_missing_not_zero(value, expected):
    assert data.parse_csv(source(weekly=value), "2026-2027")[0]["weekly_mt"] == expected


@pytest.mark.parametrize("change", ["duplicate", "period", "region", "date", "negative", "nan", "year", "header", "grade", "thousands"])
def test_bad_source_rejected(change):
    rows = list(csv.reader(io.StringIO(source().decode("utf-8-sig"))))
    fields, records = rows[0], rows[1:]
    if change == "duplicate":
        records.append(records[0])
    elif change == "period":
        records[0][5] = "Unknown"
    elif change == "region":
        records[0][8] = "Total"
    elif change == "date":
        records[0][2] = "01/08/2030"
    elif change == "negative":
        records[0][9] = "-1"
    elif change == "nan":
        records[0][9] = "NaN"
    elif change == "year":
        records[0][0] = "2025-2026"
    elif change == "header":
        fields[0] = "unknown"
    elif change == "grade":
        records[0][7] = "Total"
    else:
        records[0][9] = "1,23.4"
    with pytest.raises(ValueError):
        data.parse_csv(encode(fields, records), "2026-2027")


def test_missing_component_is_unknown_not_zero_or_partial_total():
    rows = list(csv.reader(io.StringIO(source().decode("utf-8-sig"))))
    removed = rows.pop(1)
    record = data.parse_csv(encode(rows[0], rows[1:]), "2026-2027")[0]
    assert record["weekly_mt"] is None
    assert record["weekly_mt_missing_components"] == [removed[8]]
    assert record["cumulative_mt"] == 70000


def test_source_date_typo_kept_and_marked_not_silently_corrected():
    rows = list(csv.reader(io.StringIO(source().decode("utf-8-sig"))))
    for row in rows[1:]:
        if row[1] == "3":
            row[2] = "01/08/2026"
    records = data.parse_csv(encode(rows[0], rows[1:]), "2026-2027")
    assert records[2]["date_quality"] == "non_monotonic_source_date"
    assert records[2]["week_ending"] == "2026-08-01"
    assert records[2]["weekly_mt"] == 7000


def test_complete_four_week_windows_and_exact_same_week():
    value = bundle(("2026-2027", source(weeks=(1, 2, 4, 5, 6, 7))),
                   ("2025-2026", source("2025-2026", weeks=(1, 2, 3, 4, 5, 6), cumulative="5")))
    payload = data.page_payload(value)
    points = payload["tracks"]["2026-2027"]
    assert points[2]["weekly_mt"] is None
    assert points[5]["four_week_mt"] is None
    assert points[6]["four_week_mt"] == 28000
    assert payload["previous"] == {}
    assert payload["cumulative_yoy"] is None
    assert payload["four_week_yoy"] is None
    assert payload["rank_samples"] == 1


def test_comparisons_and_rank_with_sample_count():
    value = bundle(("2026-2027", source()), ("2025-2026", source("2025-2026", cumulative="5", weekly="2")),
                   ("2024-2025", source("2024-2025", cumulative="20")))
    payload = data.page_payload(value)
    assert payload["cumulative_yoy"] == 100
    assert payload["four_week_yoy"] == -50
    assert (payload["rank"], payload["rank_samples"]) == (2, 3)
    assert data.percentage(2, 0) is None
    assert data.percentage(None, 2) is None
    assert data.page_payload(value, [])['rank'] is None


def test_season_boundaries_do_not_join_four_week_windows():
    value = bundle(("2026-2027", source(weeks=(1, 2, 3))), ("2025-2026", source("2025-2026")))
    assert data.page_payload(value)["latest"]["four_week_mt"] is None


def test_revision_backup_and_no_change_keep_stable_identity(context):
    first = update.prepare(context, {"2026-2027": source()})
    stable = update.activate_local(context, first)
    original = stable.read_bytes()
    unchanged = update.prepare(context, {"2026-2027": source()})
    update.activate_local(context, unchanged)
    assert stable.read_bytes() == original
    assert json.loads((context.runtime_root / data.STATUS).read_text())["status"] == "NO_CHANGE"
    revised = update.prepare(context, {"2026-2027": source(cumulative="11")})
    update.activate_local(context, revised)
    current = data.load_bundle(stable)
    assert len(current["revisions"]) == 5
    assert current["records"][0]["cumulative_mt"] == 77000
    assert next((context.runtime_root / 'backups/canola_exports').glob('*.json')).read_bytes() == original


def test_bad_new_snapshot_and_failure_status_preserve_stable(context):
    stable = update.activate_local(context, update.prepare(context, {"2026-2027": source()}))
    original = stable.read_bytes()
    with pytest.raises(ValueError, match="drops"):
        update.prepare(context, {"2026-2027": source(weeks=(1, 2))})
    update.record_failure(context, "source unavailable")
    assert stable.read_bytes() == original
    assert json.loads((context.runtime_root / data.STATUS).read_text())["status"] == "FAILED"


def test_candidate_baseline_compare_and_swap(context):
    first = update.prepare(context, {"2026-2027": source()})
    other = update.prepare(context, {"2026-2027": source(cumulative="20")})
    stable = update.activate_local(context, first)
    with pytest.raises(ValueError, match="baseline moved"):
        update.activate_local(context, other)
    assert data.load_bundle(stable)["records"][0]["cumulative_mt"] == 70000


def test_candidate_is_replayed_from_raw_even_if_metadata_resealed(context):
    candidate = update.prepare(context, {"2026-2027": source()})
    value = json.loads(candidate.read_text())
    value["records"][0]["weekly_mt"] = 999999
    candidate.write_text(json.dumps(value))
    meta = json.loads((candidate.parent / "candidate.json").read_text())
    meta["payload_sha256"] = data.digest(candidate.read_bytes())
    (candidate.parent / "candidate.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="reproduce"):
        update.activate_local(context, candidate)
    assert not (context.runtime_root / data.STABLE).exists()


def test_status_failure_rolls_back_atomic_stable_switch(context, monkeypatch):
    stable = update.activate_local(context, update.prepare(context, {"2026-2027": source()}))
    previous = stable.read_bytes()
    status_bytes = (context.runtime_root / data.STATUS).read_bytes()
    candidate = update.prepare(context, {"2026-2027": source(cumulative="20")})
    write = update.atomic_write_json
    def fail_status(path, payload):
        if path == context.runtime_root / data.STATUS:
            raise OSError("status write failed")
        return write(path, payload)
    monkeypatch.setattr(update, "atomic_write_json", fail_status)
    with pytest.raises(OSError):
        update.activate_local(context, candidate)
    assert stable.read_bytes() == previous
    assert (context.runtime_root / data.STATUS).read_bytes() == status_bytes


def test_locked_update_and_production_mode_rejected(context):
    with FileLock(str(context.runtime_root / "canola-exports.lock"), timeout=0):
        with pytest.raises(Timeout):
            update.prepare(context, {"2026-2027": source()})
    formal = SimpleNamespace(module_id=data.MODULE_ID, mode=RuntimeMode.PRODUCTION_WRITE)
    with pytest.raises(ValueError, match="isolated"):
        update.prepare(formal, {"2026-2027": source()})


def test_discovery_uses_only_published_official_links(monkeypatch):
    monkeypatch.setattr(update, "official_bytes", lambda *a, **k: (
        f'<a href="{data.source_url("2026-2027")}">CSV</a>'
        f'<a href="{data.source_url("2025-2026")}">CSV</a>'
        '<a href="https://evil.example/2027-28/gsw-shg-en.csv">CSV</a>').encode())
    assert update.discover_years() == ["2026-2027", "2025-2026"]


def test_bad_bundle_values_dates_and_provenance():
    good = bundle(("2026-2027", source()))
    for field, value in (("weekly_mt", float("nan")), ("cumulative_mt", -1), ("grain_week", True),
                         ("source_sha256", "0" * 64), ("week_ending", "2030-01-01")):
        bad = deepcopy(good)
        bad["records"][0][field] = value
        with pytest.raises(ValueError):
            data.validate_bundle(bad)


def test_official_excel_zero_is_evidence_not_missing_fill(context):
    raw = source(weekly=".")
    report = excel_report(week=3)
    url = data.source_url("2026-2027").rsplit("/", 1)[0] + "/03-grain-stats-weekly-2026-2027.xlsx"
    candidate = update.prepare(context, {"2026-2027": raw}, {("2026-2027", 3): (url, report)})
    stable = update.activate_local(context, candidate)
    records = data.load_bundle(stable)["records"]
    assert records[0]["weekly_mt"] is None
    assert records[2]["weekly_mt"] == 0
    assert records[2]["report_evidence"]["cells"]["weekly_mt"] == {"value": 0, "cell": "Summary!B4"}
    assert (context.runtime_root / f"raw/canola_exports/2026-2027/{data.digest(report)}.xlsx").is_file()


@pytest.mark.parametrize("year,week,value", [("2025-2026", 3, 0), ("2026-2027", 2, 0), ("2026-2027", 3, None)])
def test_excel_wrong_year_week_or_blank_not_accepted(year, week, value):
    with pytest.raises(ValueError):
        data.report_values(excel_report(year, week, value), "2026-2027", 3)


def test_excel_replay_rejects_changed_raw_source(context):
    report = excel_report(week=3)
    url = data.source_url("2026-2027").rsplit("/", 1)[0] + "/03-grain-stats-weekly-2026-2027.xlsx"
    candidate = update.prepare(context, {"2026-2027": source(weekly=".")}, {("2026-2027", 3): (url, report)})
    (context.runtime_root / f"raw/canola_exports/2026-2027/{data.digest(report)}.xlsx").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Excel source identity"):
        update.activate_local(context, candidate)
