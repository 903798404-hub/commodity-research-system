"""Test-owned sealed SQL and Public Current containing identical synthetic prices."""
from collections import defaultdict
from dataclasses import replace
import hashlib
import importlib.util
from pathlib import Path

from agri_research_agent.research_data.three_oil_v1 import load_three_oil_v1


def reference_pair(tmp_path: Path):
    root = Path(__file__).resolve().parents[2]
    script = root / "04_scripts/quality/required_lane_fixtures.py"
    spec = importlib.util.spec_from_file_location("three_oil_test_fixtures", script)
    assert spec and spec.loader
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    catalog = load_three_oil_v1()
    rows = []
    # Complete daily overlap preserves every sealed metric's expected join date.
    for template in builder._public_rows(catalog):
        if template["business_date"].day != 1:
            continue
        series = catalog.series_by_id(template["series_id"])
        for day in range(1, series.latest_date.day + 1):
            row = dict(template)
            row["business_date"] = template["business_date"].replace(day=day)
            row["value"] += day - 1
            rows.append(row)
    native = defaultdict(dict)
    for row in rows:
        series = catalog.series_by_id(row["series_id"])
        price = row["value"]
        if series.conversion_id:
            conversion = catalog.conversion_by_id(series.conversion_id)
            if conversion.operation == "multiply":
                row["value"] = price * conversion.operand
            else:
                assert conversion.operation == "divide"
                price *= conversion.operand
        native[series.source_native_table, row["business_date"]][series.source_native_series] = price
    tables = defaultdict(set)
    for series in catalog.series:
        tables[series.source_native_table].add(series.source_native_series)
    sql = []
    for table, fields in sorted(tables.items()):
        fields = sorted(fields)
        sql.append(f"CREATE TABLE `{table}` (\n`Date` date,\n" + ",\n".join(f"`{f}` decimal(24,8)" for f in fields) + "\n);\n")
        for (native_table, day), values in sorted(native.items()):
            if native_table == table:
                sql.append(f"INSERT INTO `{table}` VALUES ('{day}', " + ", ".join(str(values[f]) if f in values else "NULL" for f in fields) + ");\n")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    source = legacy / "fixture.sql"
    source.write_text("".join(sql), encoding="utf-8", newline="\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    catalog = replace(catalog, source_snapshot_sha256=digest,
        series=tuple(replace(s, source_locator=f"snapshot:fixture.sql#{s.source_native_table}") for s in catalog.series))
    builder._public_rows = lambda _catalog: rows
    public = builder._build_public_root(root, tmp_path)
    return catalog, legacy, public
