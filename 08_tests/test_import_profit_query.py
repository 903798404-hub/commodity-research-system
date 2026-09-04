from dataclasses import FrozenInstanceError
from datetime import date, datetime, time, timezone
import hashlib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.import_profit import (
    BusinessKey,
    CalculationStatus,
    SoybeanCalculationInput,
    calculate_soybean_net_crush_margin,
    load_soybean_config,
    map_soybean_contracts,
)
from agri_research_agent.import_profit.query import (
    HISTORICAL_BUSINESS_KEY_SCHEMA,
    InvalidLookbackError,
    MatrixCellStatus,
    QueryDataIntegrityError,
    QueryMetric,
    QuerySchemaError,
    UnknownMetricError,
    UnknownOriginError,
    build_recent_metric_matrix,
    iter_recent_business_weekdays,
    load_soybean_query_dataset,
)
from agri_research_agent.import_profit.result_store import (
    RESULT_SCHEMA,
    SNAPSHOT_SCHEMA,
)
from agri_research_agent.import_profit.parameter_snapshot import (
    build_parameter_snapshot,
)
from agri_research_agent.import_profit.mapping_snapshot import (
    build_mapping_snapshot,
)
from agri_research_agent.import_profit.override_snapshot import (
    build_contract_override_snapshot,
)


CALCULATED_AT = datetime(2026, 7, 30, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_soybean_config(
    ROOT / "02_configs" / "import_profit_soybean.yaml"
)
PARAMETER_HASH = build_parameter_snapshot(CONFIG).parameter_hash
MAPPING_HASH = build_mapping_snapshot(CONFIG).mapping_hash
CONTRACT_OVERRIDE_HASH = build_contract_override_snapshot(
    CONFIG
).contract_override_hash


def joined_rows(
    business_date: date,
    origin: str,
    shipment_year: int,
    shipment_month: int,
    *,
    cnf: float | None = 100.0,
    usd: float | None = 500.0,
    duty: float | None = 3500.0,
    margin: float | None = 200.0,
    cbot: float | None = 1200.0,
    fx: float | None = 7.0,
    soymeal: float | None = 3000.0,
    soyoil: float | None = 8000.0,
    missing: tuple[str, ...] | None = None,
) -> tuple[dict, dict, dict]:
    period = f"{shipment_year:04d}-{shipment_month:02d}"
    reasons = (
        tuple(missing)
        if missing is not None
        else tuple(
            reason
            for value, reason in (
                (cnf, "missing_cnf"),
                (cbot, "missing_cbot"),
                (fx, "missing_fx"),
                (soymeal, "missing_soymeal"),
                (soyoil, "missing_soyoil"),
            )
            if value is None
        )
    )
    complete = not reasons
    key = {
        "business_date": business_date,
        "commodity": "soybean",
        "origin": origin,
        "shipment_year": shipment_year,
        "shipment_month": shipment_month,
        "shipment_period": period,
        "cnf_is_null": cnf is None,
        "cnf_source": "historical_excel",
    }
    snapshot = {
        "business_date": business_date,
        "commodity": "soybean",
        "origin": origin,
        "shipment_year": shipment_year,
        "shipment_month": shipment_month,
        "shipment_period": period,
        "mapping_identity": "mapping-v1",
        "parameter_version": "1",
        "cnf_cents_per_bushel": cnf,
        "cnf_source": "historical_excel",
        "cbot_contract_year": shipment_year,
        "cbot_contract_month": shipment_month,
        "cbot_price_cents_per_bushel": cbot,
        "cbot_quality_status": "standard_window" if cbot is not None else None,
        "cbot_source": "reuters_sql" if cbot is not None else None,
        "cbot_source_snapshot_sha256": "CBOT" if cbot is not None else None,
        "fx_target_tenor": 3,
        "fx_value": fx,
        "fx_is_interpolated": False,
        "fx_lower_tenor": 3 if fx is not None else None,
        "fx_upper_tenor": 3 if fx is not None else None,
        "fx_selection_status": "direct" if fx is not None else "missing",
        "fx_source": "reuters_sql" if fx is not None else None,
        "fx_source_snapshot_sha256": "FX" if fx is not None else None,
        "soymeal_contract_code": f"M{shipment_year % 100:02d}01",
        "soymeal_price_cny_per_tonne": soymeal,
        "soymeal_price_type": (
            "historical_continuous_close" if soymeal is not None else None
        ),
        "soymeal_source": "reuters_sql" if soymeal is not None else None,
        "soymeal_contract_identity_status": (
            "continuous_inferred" if soymeal is not None else None
        ),
        "soymeal_source_contract_code": None,
        "soymeal_source_delivery_month": 1 if soymeal is not None else None,
        "soymeal_quote_date_evidence_status": (
            "source_confirmed" if soymeal is not None else None
        ),
        "soymeal_source_quote_date": (
            business_date if soymeal is not None else None
        ),
        "soymeal_source_quote_time": (
            time(23, 0) if soymeal is not None else None
        ),
        "soyoil_contract_code": f"Y{shipment_year % 100:02d}01",
        "soyoil_price_cny_per_tonne": soyoil,
        "soyoil_price_type": (
            "historical_continuous_close" if soyoil is not None else None
        ),
        "soyoil_source": "reuters_sql" if soyoil is not None else None,
        "soyoil_contract_identity_status": (
            "continuous_inferred" if soyoil is not None else None
        ),
        "soyoil_source_contract_code": None,
        "soyoil_source_delivery_month": 1 if soyoil is not None else None,
        "soyoil_quote_date_evidence_status": (
            "source_confirmed" if soyoil is not None else None
        ),
        "soyoil_source_quote_date": (
            business_date if soyoil is not None else None
        ),
        "soyoil_source_quote_time": (
            time(23, 0) if soyoil is not None else None
        ),
        "snapshot_status": "complete" if complete else "incomplete",
        "missing_reasons": list(reasons),
        "parameter_hash": PARAMETER_HASH,
        "mapping_hash": MAPPING_HASH,
        "cbot_automatic_contract_year": shipment_year,
        "cbot_automatic_contract_month": shipment_month,
        "cbot_override_contract_year": None,
        "cbot_override_contract_month": None,
        "cbot_selection_mode": "automatic",
        "cbot_override_reason": None,
        "cbot_override_effective_from": None,
        "cbot_override_effective_to": None,
        "soymeal_automatic_contract_code": f"M{shipment_year % 100:02d}01",
        "soymeal_override_contract_code": None,
        "soymeal_selection_mode": "automatic",
        "soymeal_override_reason": None,
        "soymeal_override_effective_from": None,
        "soymeal_override_effective_to": None,
        "soyoil_automatic_contract_code": f"Y{shipment_year % 100:02d}01",
        "soyoil_override_contract_code": None,
        "soyoil_selection_mode": "automatic",
        "soyoil_override_reason": None,
        "soyoil_override_effective_from": None,
        "soyoil_override_effective_to": None,
        "contract_override_hash": CONTRACT_OVERRIDE_HASH,
    }
    result = {
        "business_date": business_date,
        "commodity": "soybean",
        "origin": origin,
        "shipment_year": shipment_year,
        "shipment_month": shipment_month,
        "shipment_period": period,
        "usd_cost_per_tonne": usd if complete else None,
        "duty_paid_cost_cny_per_tonne": duty if complete else None,
        "net_crush_margin_cny_per_tonne": margin if complete else None,
        "calculation_status": "success" if complete else "incomplete",
        "missing_reasons": list(reasons),
        "parameter_version": "1",
        "mapping_identity": "mapping-v1",
        "calculated_at": CALCULATED_AT,
        "parameter_hash": PARAMETER_HASH,
        "mapping_hash": MAPPING_HASH,
        "contract_override_hash": CONTRACT_OVERRIDE_HASH,
    }
    return key, snapshot, result


def write_dataset(
    tmp_path: Path,
    records: list[tuple[dict, dict, dict]],
    *,
    key_schema: pa.Schema = HISTORICAL_BUSINESS_KEY_SCHEMA,
    snapshot_schema: pa.Schema = SNAPSHOT_SCHEMA,
    result_schema: pa.Schema = RESULT_SCHEMA,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    records = sorted(
        records,
        key=lambda parts: tuple(parts[0][name] for name in (
            "business_date", "commodity", "origin", "shipment_year",
            "shipment_month",
        )),
    )
    paths = (
        tmp_path / "historical_business_keys.parquet",
        tmp_path / "historical_soybean_market_snapshots.parquet",
        tmp_path / "historical_soybean_net_crush_results.parquet",
    )
    for path, schema, index in zip(
        paths, (key_schema, snapshot_schema, result_schema), range(3), strict=True
    ):
        names = set(schema.names)
        rows = [
            {name: value for name, value in item[index].items() if name in names}
            for item in records
        ]
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    return paths


def load_fixture(tmp_path: Path, records=None):
    values = records or [
        joined_rows(date(2026, 6, 24), "brazil", 2026, 7, cnf=0.0),
        joined_rows(date(2026, 6, 25), "brazil", 2026, 7, cnf=-5.0),
    ]
    return load_soybean_query_dataset(*write_dataset(tmp_path, values))


def test_strict_normal_load_preserves_zero_negative_and_frozen_models(tmp_path):
    dataset = load_fixture(tmp_path)
    assert dataset.business_key_count == dataset.success_count == 2
    assert dataset.incomplete_count == 0
    assert dataset.date_range == (date(2026, 6, 24), date(2026, 6, 25))
    assert [record.cnf_cents_per_bushel for record in dataset.records] == [0.0, -5.0]
    assert all("missing_cnf" not in record.missing_reasons for record in dataset.records)
    assert all("\\" not in identity.filename and "/" not in identity.filename
               for identity in (
                   dataset.business_keys_identity,
                   dataset.snapshots_identity,
                   dataset.results_identity,
               ))
    with pytest.raises(FrozenInstanceError):
        dataset.records[0].origin = "changed"
    with pytest.raises(TypeError):
        dataset._by_key[dataset.records[0].key] = dataset.records[0]


def test_success_projects_persisted_result_and_market_fields_without_changes(
    tmp_path,
):
    paths = write_dataset(
        tmp_path,
        [
            joined_rows(
                date(2026, 6, 25),
                "brazil",
                2026,
                7,
                cnf=-1.25,
                usd=411.125,
                duty=3012.75,
                margin=-8.5,
                cbot=1234.25,
                fx=6.987654,
                soymeal=0.0,
                soyoil=8123.75,
            )
        ],
    )
    before = tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in paths)
    record = load_soybean_query_dataset(*paths).records[0]
    after = tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in paths)

    assert (
        record.usd_cost_per_tonne,
        record.cbot_price_cents_per_bushel,
        record.fx_value,
        record.soymeal_price_cny_per_tonne,
        record.soyoil_price_cny_per_tonne,
    ) == (411.125, 1234.25, 6.987654, 0.0, 8123.75)
    assert (
        record.cnf_cents_per_bushel,
        record.duty_paid_cost_cny_per_tonne,
        record.net_crush_margin_cny_per_tonne,
    ) == (-1.25, 3012.75, -8.5)
    assert before == after


@pytest.mark.parametrize(
    ("missing_field", "missing_reason"),
    [
        ("cnf_cents_per_bushel", "missing_cnf"),
        ("fx_value", "missing_fx"),
        ("soymeal_price_cny_per_tonne", "missing_soymeal"),
        ("soyoil_price_cny_per_tonne", "missing_soyoil"),
    ],
)
def test_incomplete_record_preserves_all_other_persisted_market_inputs(
    tmp_path, missing_field, missing_reason
):
    values = {
        "cnf": 101.5,
        "cbot": 1201.25,
        "fx": 7.123456,
        "soymeal": 3102.5,
        "soyoil": 8203.75,
    }
    argument_for_field = {
        "cnf_cents_per_bushel": "cnf",
        "fx_value": "fx",
        "soymeal_price_cny_per_tonne": "soymeal",
        "soyoil_price_cny_per_tonne": "soyoil",
    }
    values[argument_for_field[missing_field]] = None
    dataset = load_fixture(
        tmp_path,
        [
            joined_rows(
                date(2026, 6, 25),
                "brazil",
                2026,
                7,
                **values,
            )
        ],
    )
    record = dataset.records[0]

    assert record.calculation_status == "incomplete"
    assert record.missing_reasons == (missing_reason,)
    assert record.usd_cost_per_tonne is None
    assert record.duty_paid_cost_cny_per_tonne is None
    assert record.net_crush_margin_cny_per_tonne is None
    expected = {
        "cnf_cents_per_bushel": values["cnf"],
        "cbot_price_cents_per_bushel": values["cbot"],
        "fx_value": values["fx"],
        "soymeal_price_cny_per_tonne": values["soymeal"],
        "soyoil_price_cny_per_tonne": values["soyoil"],
    }
    assert {
        field: getattr(record, field)
        for field in expected
    } == expected


@pytest.mark.parametrize(
    "snapshot_field",
    [
        "cnf_cents_per_bushel",
        "cbot_price_cents_per_bushel",
        "fx_value",
        "soymeal_price_cny_per_tonne",
        "soyoil_price_cny_per_tonne",
    ],
)
def test_complete_success_rejects_any_null_required_market_input(
    tmp_path, snapshot_field
):
    parts = list(joined_rows(date(2026, 6, 25), "brazil", 2026, 7))
    parts[1][snapshot_field] = None
    if snapshot_field == "cnf_cents_per_bushel":
        parts[0]["cnf_is_null"] = True
    paths = write_dataset(tmp_path, [tuple(parts)])
    with pytest.raises(QueryDataIntegrityError, match="market input"):
        load_soybean_query_dataset(*paths)


def test_missing_cnf_record_supplies_existing_pure_calculator_preview_input(
    tmp_path,
):
    shipment_year = 2026
    shipment_month = 12
    mapped = map_soybean_contracts(CONFIG, shipment_year, shipment_month)
    parts = list(
        joined_rows(
            date(2026, 6, 10),
            "brazil",
            shipment_year,
            shipment_month,
            cnf=None,
            cbot=1152.25,
            fx=6.691957,
            soymeal=2995.0,
            soyoil=8301.0,
        )
    )
    parts[1].update(
        {
            "mapping_identity": mapped.mapping_identity,
            "parameter_version": str(CONFIG.schema_version),
            "cbot_contract_year": mapped.cbot.contract_year,
            "cbot_contract_month": mapped.cbot.contract_month,
            "cbot_automatic_contract_year": mapped.cbot.contract_year,
            "cbot_automatic_contract_month": mapped.cbot.contract_month,
            "soymeal_contract_code": mapped.soymeal.code,
            "soymeal_automatic_contract_code": mapped.soymeal.code,
            "soyoil_contract_code": mapped.soyoil.code,
            "soyoil_automatic_contract_code": mapped.soyoil.code,
        }
    )
    parts[2].update(
        {
            "mapping_identity": mapped.mapping_identity,
            "parameter_version": str(CONFIG.schema_version),
        }
    )
    record = load_fixture(tmp_path, [tuple(parts)]).records[0]
    key = BusinessKey(
        record.business_date,
        record.commodity,
        record.origin,
        record.shipment_year,
        record.shipment_month,
        CONFIG.origin_codes,
        CONFIG.commodity,
        record.shipment_period,
    )
    assert (
        record.cbot_contract,
        record.soymeal_contract,
        record.soyoil_contract,
        record.mapping_identity,
        record.parameter_version,
    ) == (
        mapped.cbot.label,
        mapped.soymeal.code,
        mapped.soyoil.code,
        mapped.mapping_identity,
        str(CONFIG.schema_version),
    )

    preview = calculate_soybean_net_crush_margin(
        SoybeanCalculationInput(
            business_key=key,
            cnf_cents_per_bushel=125.0,
            cbot_contract=mapped.cbot,
            cbot_daily_price_cents_per_bushel=(
                record.cbot_price_cents_per_bushel
            ),
            fx_value=record.fx_value,
            soymeal_contract=mapped.soymeal,
            soymeal_price_cny_per_tonne=record.soymeal_price_cny_per_tonne,
            soyoil_contract=mapped.soyoil,
            soyoil_price_cny_per_tonne=record.soyoil_price_cny_per_tonne,
            resolved_parameters=CONFIG.resolve_parameters(record.origin),
            mapping_identity=record.mapping_identity,
            mapping_hash=record.mapping_hash,
        ),
        CONFIG,
    )
    assert preview.calculation_status is CalculationStatus.SUCCESS
    assert preview.usd_cost_per_tonne is not None
    assert preview.duty_paid_cost_cny_per_tonne is not None
    assert preview.net_crush_margin_cny_per_tonne is not None


def test_query_layer_neither_calls_calculator_nor_copies_business_formula():
    source = (
        ROOT / "03_src" / "agri_research_agent" / "import_profit" / "query.py"
    ).read_text(encoding="utf-8")
    assert "calculate_soybean_net_crush_margin" not in source
    assert "SoybeanCalculationInput" not in source
    assert "cents_per_bushel_to_usd_per_tonne" not in source
    assert "meal_yield" not in source
    assert "oil_yield" not in source


@pytest.mark.parametrize("failure", ["missing", "order", "type"])
def test_schema_missing_reordered_or_wrong_type_is_rejected(tmp_path, failure):
    schema = SNAPSHOT_SCHEMA
    if failure == "missing":
        schema = schema.remove(8)
    elif failure == "order":
        fields = list(schema)
        schema = pa.schema([fields[1], fields[0], *fields[2:]])
    else:
        fields = list(schema)
        fields[4] = pa.field("shipment_month", pa.int16(), nullable=False)
        schema = pa.schema(fields)
    paths = write_dataset(
        tmp_path,
        [joined_rows(date(2026, 6, 25), "brazil", 2026, 7)],
        snapshot_schema=schema,
    )
    with pytest.raises(QuerySchemaError, match="Schema"):
        load_soybean_query_dataset(*paths)


def test_duplicate_and_key_set_mismatch_are_rejected(tmp_path):
    item = joined_rows(date(2026, 6, 25), "brazil", 2026, 7)
    paths = write_dataset(tmp_path / "duplicate", [item, item])
    with pytest.raises(QueryDataIntegrityError, match="duplicate"):
        load_soybean_query_dataset(*paths)

    first = joined_rows(date(2026, 6, 25), "brazil", 2026, 7)
    second = joined_rows(date(2026, 6, 25), "brazil", 2026, 8)
    paths = write_dataset(tmp_path / "mismatch", [first])
    pq.write_table(
        pa.Table.from_pylist([second[2]], schema=RESULT_SCHEMA), paths[2]
    )
    with pytest.raises(QueryDataIntegrityError, match="identical"):
        load_soybean_query_dataset(*paths)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("status", "statuses"),
        ("success_null", "successful"),
        ("incomplete_value", "incomplete calculation"),
        ("missing", "missing reasons"),
        ("parameter", "parameter"),
        ("mapping", "mapping"),
        ("period", "shipment_period"),
    ],
)
def test_join_integrity_failures_are_never_silently_repaired(
    tmp_path, mutation, match
):
    parts = list(joined_rows(date(2026, 6, 25), "brazil", 2026, 7))
    if mutation == "status":
        parts[2]["calculation_status"] = "incomplete"
        for field in (
            "usd_cost_per_tonne",
            "duty_paid_cost_cny_per_tonne",
            "net_crush_margin_cny_per_tonne",
        ):
            parts[2][field] = None
    elif mutation == "success_null":
        parts[2]["net_crush_margin_cny_per_tonne"] = None
    elif mutation == "incomplete_value":
        parts = list(joined_rows(
            date(2026, 6, 25), "brazil", 2026, 7,
            cnf=None, duty=None, margin=None,
        ))
        parts[2]["net_crush_margin_cny_per_tonne"] = 0.0
    elif mutation == "missing":
        parts[2]["missing_reasons"] = ["missing_fx"]
    elif mutation == "parameter":
        parts[2]["parameter_version"] = "2"
    elif mutation == "mapping":
        parts[2]["mapping_identity"] = "other"
    else:
        parts[2]["shipment_period"] = "2026-08"
    paths = write_dataset(tmp_path, [tuple(parts)])
    with pytest.raises(QueryDataIntegrityError, match=match):
        load_soybean_query_dataset(*paths)


@pytest.mark.parametrize(
    ("as_of", "count", "expected"),
    [
        (date(2026, 6, 22), 1, (date(2026, 6, 22),)),
        (
            date(2026, 6, 26),
            10,
            (
                date(2026, 6, 26), date(2026, 6, 25),
                date(2026, 6, 24), date(2026, 6, 23),
                date(2026, 6, 22), date(2026, 6, 19),
                date(2026, 6, 18), date(2026, 6, 17),
                date(2026, 6, 16), date(2026, 6, 15),
            ),
        ),
        (
            date(2026, 1, 2),
            3,
            (date(2026, 1, 2), date(2026, 1, 1), date(2025, 12, 31)),
        ),
    ],
)
def test_recent_weekday_axis_includes_as_of_and_keeps_weekday_holidays(
    as_of, count, expected
):
    assert iter_recent_business_weekdays(as_of, count) == expected


@pytest.mark.parametrize(
    ("as_of", "count"),
    [
        (date(2026, 6, 27), 10),
        (date(2026, 6, 25), 0),
        (date(2026, 6, 25), -1),
        (date(2026, 6, 25), 261),
    ],
)
def test_invalid_recent_weekday_requests_fail(as_of, count):
    with pytest.raises(InvalidLookbackError):
        iter_recent_business_weekdays(as_of, count)


def test_recent_matrices_keep_real_years_missing_rows_and_display_only_values(
    tmp_path,
):
    records = []
    for origin in ("argentina", "brazil", "us_gulf", "us_pnw"):
        records.extend(
            [
                joined_rows(
                    date(2026, 7, 31), origin, 2026, 8,
                    cnf=0.0, duty=3100.0, margin=-20.0,
                ),
                joined_rows(
                    date(2026, 8, 3), origin, 2027, 8,
                    cnf=-2.0, duty=3200.0, margin=30.0,
                ),
            ]
        )
    dataset = load_fixture(tmp_path, records)
    before = dataset.records
    matrices = {
        metric: build_recent_metric_matrix(
            dataset,
            origin="brazil",
            as_of_date=date(2026, 8, 3),
            metric=metric,
            lookback_days=2,
        )
        for metric in QueryMetric
    }
    cnf = matrices[QueryMetric.CNF]
    assert cnf.row_order == "descending"
    assert [row.business_date for row in cnf.rows] == [
        date(2026, 8, 3), date(2026, 7, 31)
    ]
    assert cnf.column_months == tuple(range(1, 13))
    assert all(len(row.cells) == 12 for row in cnf.rows)
    assert cnf.get_cell(date(2026, 7, 31), 8).shipment_period == "2026-08"
    assert cnf.get_cell(date(2026, 8, 3), 8).shipment_period == "2027-08"
    assert cnf.get_cell(date(2026, 7, 31), 8).value == 0.0
    assert cnf.get_cell(date(2026, 8, 3), 8).value == -2.0
    assert cnf.get_cell(date(2026, 8, 3), 7).status is (
        MatrixCellStatus.MISSING_SHIPMENT_PERIOD
    )
    assert matrices[QueryMetric.DUTY_PAID_COST].get_cell(
        date(2026, 8, 3), 8
    ).value == 3200.0
    assert matrices[QueryMetric.NET_CRUSH_MARGIN].get_cell(
        date(2026, 7, 31), 8
    ).value == -20.0
    frame = cnf.to_dataframe()
    assert list(frame.columns) == [
        "日期", *(f"{month}月船期" for month in range(1, 13))
    ]
    assert "shipment_year" not in frame.columns
    assert dataset.records == before
    for origin in dataset.origins:
        assert build_recent_metric_matrix(
            dataset, origin=origin, as_of_date=date(2026, 8, 3),
            metric="cnf", lookback_days=1,
        ).origin == origin


def test_missing_business_day_and_null_metric_remain_null_without_fill(tmp_path):
    incomplete = joined_rows(
        date(2026, 6, 25), "brazil", 2026, 7,
        cnf=None, duty=None, margin=None,
    )
    dataset = load_fixture(tmp_path, [incomplete])
    matrix = build_recent_metric_matrix(
        dataset,
        origin="brazil",
        as_of_date=date(2026, 6, 25),
        metric="net_crush_margin",
        lookback_days=2,
    )
    assert matrix.get_cell(date(2026, 6, 25), 7).value is None
    assert matrix.get_cell(date(2026, 6, 25), 7).status is (
        MatrixCellStatus.MISSING_VALUE
    )
    assert all(
        cell.value is None
        and cell.status is MatrixCellStatus.MISSING_BUSINESS_DATE
        for cell in matrix.rows[1].cells
    )
    for metric in QueryMetric:
        metric_matrix = build_recent_metric_matrix(
            dataset,
            origin="brazil",
            as_of_date=date(2026, 6, 25),
            metric=metric,
            lookback_days=1,
        )
        assert metric_matrix.get_cell(date(2026, 6, 25), 7).value is None


def test_recent_matrix_defaults_to_ten_business_weekdays(tmp_path):
    dataset = load_fixture(tmp_path)
    matrix = build_recent_metric_matrix(
        dataset,
        origin="brazil",
        as_of_date=date(2026, 6, 25),
        metric="cnf",
    )
    assert matrix.lookback_days == 10
    assert len(matrix.rows) == 10
    assert matrix.rows[0].business_date == date(2026, 6, 25)


def test_unknown_origin_and_metric_are_controlled_errors(tmp_path):
    dataset = load_fixture(tmp_path)
    with pytest.raises(UnknownOriginError):
        build_recent_metric_matrix(
            dataset, origin="unknown", as_of_date=date(2026, 6, 25),
            metric="cnf",
        )
    with pytest.raises(UnknownMetricError):
        build_recent_metric_matrix(
            dataset, origin="brazil", as_of_date=date(2026, 6, 25),
            metric="arbitrary_column",
        )
