from __future__ import annotations

from pathlib import Path

import yaml
from agri_research_agent.data_sources.lutou import weather_live


class CatalogClient:
    def __init__(self, policy_path: Path) -> None:
        self.inventory_count = 565
        self.inventory_calls = 0
        self.date_bounds_calls = 0
        self.relation_calls = 0
        payload = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        self.columns: dict[str, list[str]] = {}
        for route in payload["routes"]:
            config = weather_live.load_weather_config(
                policy_path.parent / route["config"]
            )
            prefix = route.get("source_table_prefix") or config.get(
                "source_table_prefix"
            )
            specs = weather_live._table_specs(
                prefix, minimum_temperature=bool(route["minimum_temperature"])
            )
            for table, spec in specs.items():
                expected, _ = weather_live._expected_columns(config, spec)
                actual = weather_live._sealed_actual_columns(
                    expected, prefix, route
                )
                existing = self.columns.get(table)
                if existing is not None:
                    assert existing == actual
                self.columns[table] = actual

    def inspect_schema_inventory(self, schema):  # type: ignore[no-untyped-def]
        assert schema == "天气2.0"
        self.inventory_calls += 1
        mapped = [
            {
                "TABLE_NAME": table,
                "TABLE_TYPE": "BASE TABLE",
                "TABLE_ROWS": 10,
                "TABLE_COMMENT": "",
                "UPDATE_TIME": None,
            }
            for table in self.columns
        ]
        mapped.extend(
            {
                "TABLE_NAME": f"out-of-scope-{index}",
                "TABLE_TYPE": "BASE TABLE",
                "TABLE_ROWS": 0,
                "TABLE_COMMENT": "",
                "UPDATE_TIME": None,
            }
            for index in range(self.inventory_count - len(mapped))
        )
        return tuple(mapped)

    def inspect_relation(self, schema, table):  # type: ignore[no-untyped-def]
        self.relation_calls += 1
        return tuple(
            {
                "COLUMN_NAME": column,
                "DATA_TYPE": "date" if index == 0 else "double",
                "IS_NULLABLE": "YES",
                "ORDINAL_POSITION": index + 1,
                "COLUMN_COMMENT": "",
            }
            for index, column in enumerate(self.columns[table])
        )

    def inspect_query(self, query):  # type: ignore[no-untyped-def]
        assert {query.date_column, *query.value_columns} <= set(
            self.columns[query.table]
        )
        return ()

    def date_bounds(self, query, *, inspect=True):  # type: ignore[no-untyped-def]
        self.date_bounds_calls += 1
        raise AssertionError("daily sealed mapping must not query relation bounds")


def test_current_consumer_weather_mapping_is_exact_and_complete() -> None:
    policy = Path("02_configs/lutou_weather_current.yaml")
    client = CatalogClient(policy)
    catalog = weather_live.load_weather_source_catalog(client, policy)

    assert client.inventory_calls == 0
    assert client.date_bounds_calls == 0
    assert client.relation_calls == 0
    assert catalog.schema_inventory == ()
    assert len(catalog.tables) == 90
    assert len(catalog.series) == 686
    assert len({item.series_id for item in catalog.series}) == 686
    assert {item.model for item in catalog.series} == {"OBSERVED", "ECMWF", "GFS"}
    assert catalog.forecast_horizon_days == 15
    assert {item.unit for item in catalog.series if item.metric == "precipitation"} == {
        "mm"
    }
    assert {
        item.unit for item in catalog.series if item.metric.startswith("temperature")
    } == {"degC"}

    canada = [item for item in catalog.series if item.country == "CAN"]
    assert canada
    assert {item.region_type for item in canada} == {"source_provided_weighted_region"}
    assert {item.region for item in canada} == {
        "saskatchewan_weighted",
        "alberta_weighted",
        "manitoba_weighted",
    }
    assert all("SK.9A" not in item.series_id for item in canada)


def test_forecast_identity_separates_model_and_declares_missing_issue_date() -> None:
    policy_path = Path("02_configs/lutou_weather_current.yaml")
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert policy["forecast_issue_policy"] == {
        "issue_date_field": None,
        "issue_date_status": "SOURCE_NOT_PROVIDED",
        "run_identity": "source_content_sha256",
        "valid_date_field": "日期",
    }
    catalog = weather_live.load_weather_source_catalog(
        CatalogClient(policy_path), policy_path
    )
    sample = [
        item
        for item in catalog.series
        if item.crop == "soybean"
        and item.country == "USA"
        and item.region == "illinois"
        and item.metric == "precipitation"
    ]
    assert {(item.data_family, item.model) for item in sample} == {
        ("observation", "OBSERVED"),
        ("forecast", "ECMWF"),
        ("forecast", "GFS"),
    }
    assert len({item.series_id for item in sample}) == 3


def test_inventory_discovery_is_explicit_audit_only() -> None:
    policy = Path("02_configs/lutou_weather_current.yaml")
    client = CatalogClient(policy)
    client.inventory_count = 566

    daily_catalog = weather_live.load_weather_source_catalog(client, policy)

    assert client.inventory_calls == 0
    assert daily_catalog.safe_manifest_fields()["inventory_mode"] == (
        "SEALED_AUDIT_EVIDENCE"
    )
    assert daily_catalog.safe_manifest_fields()["out_of_scope_table_count"] == 464

    audit_catalog = weather_live.load_weather_source_catalog(
        client, policy, audit_inventory=True, audit_relations=True
    )

    assert client.inventory_calls == 1
    assert client.relation_calls == 90
    assert len(audit_catalog.schema_inventory) == 566
    assert len(audit_catalog.tables) == 90
    assert len(audit_catalog.series) == 686
    assert audit_catalog.safe_manifest_fields()["inventory_mode"] == (
        "LIVE_SCHEMA_AUDIT"
    )
    assert audit_catalog.safe_manifest_fields()["out_of_scope_table_count"] == 465
