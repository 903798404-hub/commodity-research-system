from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import time
from pathlib import Path

import pytest
import yaml

from agri_research_agent.import_profit.config import (
    ImportProfitConfigError,
    load_soybean_config,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = ROOT / "02_configs" / "import_profit_soybean.yaml"


def write_variant(tmp_path: Path, mutate) -> Path:
    payload = yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))
    mutate(payload)
    target = tmp_path / "import_profit_soybean.yaml"
    target.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target


def test_real_config_loads_into_immutable_models() -> None:
    config = load_soybean_config(REAL_CONFIG)

    assert config.schema_version == 1
    assert config.commodity == "soybean"
    assert config.origin_codes == ("brazil", "us_gulf", "us_pnw", "argentina")
    assert len(config.contract_mapping) == 12
    assert config.default_parameters.meal_yield == 0.785
    assert config.origin_overrides == ()
    assert config.business_calendar_policy.timezone == "Asia/Shanghai"
    assert config.business_calendar_policy.calendar_type == "weekday"
    assert config.business_calendar_policy.weekdays == (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
    )
    assert config.business_calendar_policy.exchange_holiday_calendar_required is False
    assert config.business_calendar_policy.retain_weekday_without_market_data is True
    assert config.business_calendar_policy.allow_previous_business_day_fallback is False
    assert config.dce_daily_policy.price_type == "night_session_close"
    assert config.dce_daily_policy.source_function == "futures_zh_spot"
    assert config.dce_daily_policy.price_field == "current_price"
    assert config.dce_daily_policy.capture_timezone == "Asia/Shanghai"
    assert config.dce_daily_policy.scheduled_time == time(8, 30)
    assert config.dce_daily_policy.capture_start == time(8, 30)
    assert config.dce_daily_policy.capture_end_exclusive == time(8, 33)
    assert config.dce_daily_policy.source_quote_start == time(22, 59)
    assert config.dce_daily_policy.source_quote_end_exclusive == time(23, 1)
    assert config.dce_daily_policy.trade_calendar_function == (
        "tool_trade_date_hist_sina"
    )
    assert config.dce_daily_policy.require_same_business_date is True
    assert config.dce_daily_policy.require_complete_contract_set is False
    assert config.dce_daily_policy.allow_previous_date_fallback is False
    assert config.dce_daily_policy.allow_post_close_fallback is False
    assert config.dce_daily_policy.allow_historical_close_fallback is False
    assert config.dce_daily_policy.allow_adjacent_contract_fallback is False
    with pytest.raises(FrozenInstanceError):
        config.commodity = "rapeseed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        config.origins[0] = config.origins[0]  # type: ignore[index]


def test_missing_file_and_invalid_yaml_fail_clearly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_soybean_config(tmp_path / "missing.yaml")

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: [\n", encoding="utf-8")
    with pytest.raises(ImportProfitConfigError, match="invalid YAML"):
        load_soybean_config(invalid)


def test_missing_or_unknown_critical_fields_are_rejected(tmp_path: Path) -> None:
    missing = write_variant(tmp_path, lambda payload: payload.pop("schema_version"))
    with pytest.raises(ImportProfitConfigError, match="missing keys"):
        load_soybean_config(missing)

    unknown = write_variant(tmp_path, lambda payload: payload.update({"unexpected_policy": {}}))
    with pytest.raises(ImportProfitConfigError, match="unknown keys"):
        load_soybean_config(unknown)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timezone", "UTC"),
        ("calendar_type", "exchange"),
        ("weekdays", ["monday", "friday"]),
        ("exclude_weekends", False),
        ("exchange_holiday_calendar_required", True),
        ("retain_weekday_without_market_data", False),
        ("infer_exchange_open_from_market_data", True),
        ("allow_previous_business_day_fallback", True),
    ],
)
def test_weekday_calendar_policy_is_strict(
    tmp_path: Path, field: str, value: object
) -> None:
    path = write_variant(
        tmp_path,
        lambda payload: payload["business_calendar_policy"].update({field: value}),
    )
    with pytest.raises(ImportProfitConfigError, match="business calendar safety"):
        load_soybean_config(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_type", "historical_continuous_close"),
        ("capture_timezone", "UTC"),
        ("require_same_business_date", False),
        ("require_complete_contract_set", True),
        ("allow_previous_date_fallback", True),
        ("allow_post_close_fallback", True),
        ("allow_historical_close_fallback", True),
        ("allow_adjacent_contract_fallback", True),
        ("allow_other_price_field_fallback", True),
    ],
)
def test_dce_night_session_close_policy_is_strict(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    path = write_variant(
        tmp_path,
        lambda payload: payload["source_policy"]["dce"][
            "incremental_source"
        ].update({field: value}),
    )
    with pytest.raises(ImportProfitConfigError, match="night-session-close"):
        load_soybean_config(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("start", "08:29:59"), ("end_exclusive", "08:33:01")],
)
def test_dce_capture_window_is_strict(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    path = write_variant(
        tmp_path,
        lambda payload: payload["source_policy"]["dce"]["incremental_source"][
            "capture_window"
        ].update({field: value}),
    )
    with pytest.raises(ImportProfitConfigError, match="night-session-close"):
        load_soybean_config(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("meal_yield", 0),
        ("oil_yield", -0.1),
        ("cents_per_bushel_to_usd_per_tonne", float("nan")),
        ("tariff_rate", -0.01),
        ("port_charge_cny_per_tonne", "100"),
    ],
)
def test_invalid_default_parameters_are_rejected(tmp_path: Path, field: str, value: object) -> None:
    path = write_variant(tmp_path, lambda payload: payload["default_parameters"].update({field: value}))
    with pytest.raises(ImportProfitConfigError):
        load_soybean_config(path)


def test_unknown_origin_fails_and_override_inherits_unspecified_defaults(tmp_path: Path) -> None:
    path = write_variant(
        tmp_path,
        lambda payload: payload["origin_overrides"].update(
            {"brazil": {"port_charge_cny_per_tonne": 125}}
        ),
    )
    config = load_soybean_config(path)
    brazil = config.resolve_parameters("brazil")
    us_gulf = config.resolve_parameters("us_gulf")

    assert brazil.port_charge_cny_per_tonne == 125
    assert brazil.processing_fee_cny_per_tonne == config.default_parameters.processing_fee_cny_per_tonne
    assert brazil.meal_yield == config.default_parameters.meal_yield
    assert us_gulf == config.default_parameters
    with pytest.raises(ImportProfitConfigError, match="unknown origin"):
        config.resolve_parameters("unknown")


def test_unknown_override_origin_or_parameter_is_rejected(tmp_path: Path) -> None:
    unknown_origin = write_variant(
        tmp_path,
        lambda payload: payload["origin_overrides"].update({"unknown": {"tariff_rate": 0.04}}),
    )
    with pytest.raises(ImportProfitConfigError, match="unknown origins"):
        load_soybean_config(unknown_origin)

    unknown_field = write_variant(
        tmp_path,
        lambda payload: payload["origin_overrides"].update({"brazil": {"fictional_fee": 1}}),
    )
    with pytest.raises(ImportProfitConfigError, match="unknown fields"):
        load_soybean_config(unknown_field)
