"""Strict, read-only loader for the soybean import-profit contract."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import time
from math import isfinite
from pathlib import Path
from typing import Any, Mapping

import yaml


class ImportProfitConfigError(ValueError):
    pass


PARAMETER_FIELDS = (
    "meal_yield",
    "oil_yield",
    "cents_per_bushel_to_usd_per_tonne",
    "tariff_rate",
    "vat_rate",
    "port_charge_cny_per_tonne",
    "processing_fee_cny_per_tonne",
    "additional_fees_cny_per_tonne",
)
REQUIRED_STABLE_KEY = (
    "business_date",
    "commodity",
    "origin",
    "shipment_year",
    "shipment_month",
)
EXPECTED_ORIGINS = (
    ("brazil", "巴西"),
    ("us_gulf", "美湾"),
    ("us_pnw", "美西"),
    ("argentina", "阿根廷"),
)
TOP_LEVEL_KEYS = {
    "schema_version",
    "framework",
    "commodity",
    "page_title",
    "origins",
    "default_parameters",
    "origin_overrides",
    "formula_policy",
    "cnf_unit",
    "stable_key",
    "shipment_period",
    "cnf_policy",
    "fx_policy",
    "business_calendar_policy",
    "contract_mapping",
    "display_policy",
    "seasonality_policy",
    "source_policy",
}


@dataclass(frozen=True, slots=True)
class Origin:
    code: str
    label: str


@dataclass(frozen=True, slots=True)
class SoybeanParameters:
    meal_yield: float
    oil_yield: float
    cents_per_bushel_to_usd_per_tonne: float
    tariff_rate: float
    vat_rate: float
    port_charge_cny_per_tonne: float
    processing_fee_cny_per_tonne: float
    additional_fees_cny_per_tonne: float

    def __post_init__(self) -> None:
        positive = {"meal_yield", "oil_yield", "cents_per_bushel_to_usd_per_tonne"}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
                raise ImportProfitConfigError(f"default_parameters.{item.name} must be finite")
            if item.name in positive and value <= 0:
                raise ImportProfitConfigError(f"default_parameters.{item.name} must be positive")
            if item.name not in positive and value < 0:
                raise ImportProfitConfigError(f"default_parameters.{item.name} must be non-negative")
            object.__setattr__(self, item.name, float(value))


@dataclass(frozen=True, slots=True)
class ParameterOverride:
    values: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class ContractLegRule:
    contract_month: int
    year_offset: int


@dataclass(frozen=True, slots=True)
class ContractMappingRule:
    shipment_month: int
    cbot: ContractLegRule
    dce: ContractLegRule


@dataclass(frozen=True, slots=True)
class FxPolicy:
    minimum_tenor_months: int
    maximum_tenor_months: int
    tenor_columns: tuple[tuple[int, str], ...]
    exact_tenor_first: bool
    interpolation_method: str
    same_business_date_only: bool
    adjacent_valid_tenors_only: bool
    extrapolation_allowed: bool


@dataclass(frozen=True, slots=True)
class BusinessCalendarPolicy:
    timezone: str
    calendar_type: str
    weekdays: tuple[str, ...]
    exclude_weekends: bool
    exchange_holiday_calendar_required: bool
    retain_weekday_without_market_data: bool
    infer_exchange_open_from_market_data: bool
    allow_previous_business_day_fallback: bool


@dataclass(frozen=True, slots=True)
class DisplayPolicy:
    application: str
    displayed_profit_metrics: tuple[str, ...]
    forbidden_profit_metrics: tuple[str, ...]
    recent_business_days: int


@dataclass(frozen=True, slots=True)
class SeasonalityPolicy:
    window_start: str
    window_end: str
    prior_shipment_years: int
    prior_year_mean_years: int
    minimum_valid_years: int
    smoothing_enabled: bool
    null_line_break: bool


@dataclass(frozen=True, slots=True)
class DceDailyPolicy:
    provider: str
    source_function: str
    price_field: str
    price_type: str
    capture_timezone: str
    scheduled_time: time
    capture_start: time
    capture_end_exclusive: time
    source_quote_start: time
    source_quote_end_exclusive: time
    trade_calendar_function: str
    require_same_business_date: bool
    require_complete_contract_set: bool
    allow_previous_date_fallback: bool
    allow_post_close_fallback: bool
    allow_historical_close_fallback: bool
    allow_adjacent_contract_fallback: bool
    allow_other_price_field_fallback: bool


DEFAULT_DCE_DAILY_POLICY = DceDailyPolicy(
    provider="akshare",
    source_function="futures_zh_spot",
    price_field="current_price",
    price_type="night_session_close",
    capture_timezone="Asia/Shanghai",
    scheduled_time=time(8, 30),
    capture_start=time(8, 30),
    capture_end_exclusive=time(8, 33),
    source_quote_start=time(22, 59),
    source_quote_end_exclusive=time(23, 1),
    trade_calendar_function="tool_trade_date_hist_sina",
    require_same_business_date=True,
    require_complete_contract_set=False,
    allow_previous_date_fallback=False,
    allow_post_close_fallback=False,
    allow_historical_close_fallback=False,
    allow_adjacent_contract_fallback=False,
    allow_other_price_field_fallback=False,
)


@dataclass(frozen=True, slots=True)
class SoybeanImportProfitConfig:
    schema_version: int
    commodity: str
    page_title: str
    origins: tuple[Origin, ...]
    default_parameters: SoybeanParameters
    origin_overrides: tuple[tuple[str, ParameterOverride], ...]
    cnf_unit: str
    stable_key: tuple[str, ...]
    contract_mapping: tuple[ContractMappingRule, ...]
    contract_mapping_identity: str
    fx_policy: FxPolicy
    business_calendar_policy: BusinessCalendarPolicy
    display_policy: DisplayPolicy
    seasonality_policy: SeasonalityPolicy
    dce_daily_policy: DceDailyPolicy = DEFAULT_DCE_DAILY_POLICY

    @property
    def origin_codes(self) -> tuple[str, ...]:
        return tuple(origin.code for origin in self.origins)

    def resolve_parameters(self, origin: str) -> SoybeanParameters:
        if origin not in self.origin_codes:
            raise ImportProfitConfigError(f"unknown origin: {origin}")
        overrides = dict(self.origin_overrides)
        override = overrides.get(origin)
        if override is None:
            return self.default_parameters
        return replace(self.default_parameters, **dict(override.values))


def load_soybean_config(path: str | Path) -> SoybeanImportProfitConfig:
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8", errors="strict")
    except FileNotFoundError:
        raise
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ImportProfitConfigError(f"invalid YAML: {config_path}") from exc
    root = _mapping(payload, "root")
    _require_exact_keys(root, TOP_LEVEL_KEYS, "root")

    schema_version = _integer(root["schema_version"], "schema_version")
    if schema_version <= 0:
        raise ImportProfitConfigError("schema_version must be positive")
    if root["commodity"] != "soybean":
        raise ImportProfitConfigError("commodity must be soybean")
    if not isinstance(root["page_title"], str) or not root["page_title"].strip():
        raise ImportProfitConfigError("page_title must be non-empty")

    origins = _parse_origins(root["origins"])
    parameters = _parse_parameters(root["default_parameters"], "default_parameters")
    overrides = _parse_overrides(root["origin_overrides"], origins)
    stable_key = tuple(_list(root["stable_key"], "stable_key"))
    if stable_key != REQUIRED_STABLE_KEY:
        raise ImportProfitConfigError("stable_key is incomplete or reordered")
    if root["cnf_unit"] != "cents_per_bushel":
        raise ImportProfitConfigError("cnf_unit must be cents_per_bushel")

    mapping = _parse_contract_mapping(root["contract_mapping"], schema_version)
    fx_policy = _parse_fx_policy(root["fx_policy"])
    calendar_policy = _parse_calendar_policy(root["business_calendar_policy"])
    display_policy = _parse_display_policy(root["display_policy"])
    seasonality_policy = _parse_seasonality_policy(root["seasonality_policy"])
    dce_daily_policy = _parse_dce_daily_policy(root["source_policy"])

    return SoybeanImportProfitConfig(
        schema_version=schema_version,
        commodity="soybean",
        page_title=root["page_title"],
        origins=origins,
        default_parameters=parameters,
        origin_overrides=overrides,
        cnf_unit="cents_per_bushel",
        stable_key=stable_key,
        contract_mapping=mapping,
        contract_mapping_identity=f"import_profit_soybean:schema_version={schema_version}",
        fx_policy=fx_policy,
        business_calendar_policy=calendar_policy,
        display_policy=display_policy,
        seasonality_policy=seasonality_policy,
        dce_daily_policy=dce_daily_policy,
    )


def _parse_origins(value: object) -> tuple[Origin, ...]:
    items = _list(value, "origins")
    origins: list[Origin] = []
    for index, item in enumerate(items):
        origin = _mapping(item, f"origins[{index}]")
        _require_exact_keys(origin, {"code", "label"}, f"origins[{index}]")
        origins.append(Origin(str(origin["code"]), str(origin["label"])))
    actual = tuple((origin.code, origin.label) for origin in origins)
    if actual != EXPECTED_ORIGINS:
        raise ImportProfitConfigError("origins must contain the four configured origins in order")
    return tuple(origins)


def _parse_parameters(value: object, path: str) -> SoybeanParameters:
    raw = _mapping(value, path)
    _require_exact_keys(raw, set(PARAMETER_FIELDS), path)
    return SoybeanParameters(**{name: raw[name] for name in PARAMETER_FIELDS})


def _parse_overrides(
    value: object,
    origins: tuple[Origin, ...],
) -> tuple[tuple[str, ParameterOverride], ...]:
    raw = _mapping(value, "origin_overrides")
    allowed_origins = {origin.code for origin in origins}
    unknown_origins = set(raw) - allowed_origins
    if unknown_origins:
        raise ImportProfitConfigError(f"origin_overrides contains unknown origins: {sorted(unknown_origins)}")
    parsed: list[tuple[str, ParameterOverride]] = []
    for origin_code, override_value in raw.items():
        override = _mapping(override_value, f"origin_overrides.{origin_code}")
        unknown_fields = set(override) - set(PARAMETER_FIELDS)
        if unknown_fields:
            raise ImportProfitConfigError(
                f"origin_overrides.{origin_code} contains unknown fields: {sorted(unknown_fields)}"
            )
        values: list[tuple[str, float]] = []
        for key, raw_value in override.items():
            candidate = replace(
                SoybeanParameters(
                    meal_yield=1,
                    oil_yield=1,
                    cents_per_bushel_to_usd_per_tonne=1,
                    tariff_rate=0,
                    vat_rate=0,
                    port_charge_cny_per_tonne=0,
                    processing_fee_cny_per_tonne=0,
                    additional_fees_cny_per_tonne=0,
                ),
                **{key: raw_value},
            )
            values.append((key, getattr(candidate, key)))
        parsed.append((origin_code, ParameterOverride(tuple(values))))
    return tuple(parsed)


def _parse_contract_mapping(value: object, schema_version: int) -> tuple[ContractMappingRule, ...]:
    raw = _mapping(value, "contract_mapping")
    _require_exact_keys(
        raw,
        {"contract_year_expression", "code_policy", "shared_across", "rows"},
        "contract_mapping",
    )
    if raw["contract_year_expression"] != "shipment_year + year_offset":
        raise ImportProfitConfigError("contract_year_expression is unsupported")
    rows = _list(raw["rows"], "contract_mapping.rows")
    parsed: list[ContractMappingRule] = []
    for index, item in enumerate(rows):
        row = _mapping(item, f"contract_mapping.rows[{index}]")
        _require_exact_keys(row, {"shipment_month", "cbot", "dce"}, f"contract_mapping.rows[{index}]")
        shipment_month = _month(row["shipment_month"], f"contract_mapping.rows[{index}].shipment_month")
        parsed.append(
            ContractMappingRule(
                shipment_month=shipment_month,
                cbot=_parse_contract_leg(row["cbot"], f"contract_mapping.rows[{index}].cbot"),
                dce=_parse_contract_leg(row["dce"], f"contract_mapping.rows[{index}].dce"),
            )
        )
    months = [rule.shipment_month for rule in parsed]
    if sorted(months) != list(range(1, 13)) or len(set(months)) != 12:
        raise ImportProfitConfigError("contract_mapping must contain each shipment month exactly once")
    if schema_version <= 0:
        raise ImportProfitConfigError("mapping requires a positive schema_version")
    return tuple(sorted(parsed, key=lambda rule: rule.shipment_month))


def _parse_contract_leg(value: object, path: str) -> ContractLegRule:
    raw = _mapping(value, path)
    _require_exact_keys(raw, {"contract_month", "year_offset"}, path)
    month = _month(raw["contract_month"], f"{path}.contract_month")
    offset = _integer(raw["year_offset"], f"{path}.year_offset")
    if offset not in {0, 1}:
        raise ImportProfitConfigError(f"{path}.year_offset must be 0 or 1")
    return ContractLegRule(month, offset)


def _parse_fx_policy(value: object) -> FxPolicy:
    raw = _mapping(value, "fx_policy")
    required = {
        "source_file",
        "source_table",
        "value_policy",
        "distinguish_bid_ask_mid",
        "tenor_basis",
        "tenor_start",
        "tenor_end",
        "minimum_tenor_months",
        "maximum_tenor_months",
        "reject_negative_tenor",
        "reject_above_maximum_tenor",
        "tenor_columns",
        "exact_tenor_first",
        "interpolation",
    }
    _require_exact_keys(raw, required, "fx_policy")
    minimum = _integer(raw["minimum_tenor_months"], "fx_policy.minimum_tenor_months")
    maximum = _integer(raw["maximum_tenor_months"], "fx_policy.maximum_tenor_months")
    if (minimum, maximum) != (0, 12):
        raise ImportProfitConfigError("fx_policy tenor range must be 0 through 12")
    columns = _mapping(raw["tenor_columns"], "fx_policy.tenor_columns")
    expected_columns = {0: "Spot", **{month: f"Fwd_{month}M" for month in range(1, 12)}, 12: "Fwd_1Y"}
    if columns != expected_columns:
        raise ImportProfitConfigError("fx_policy.tenor_columns must cover Spot through Fwd_1Y")
    interpolation = _mapping(raw["interpolation"], "fx_policy.interpolation")
    _require_exact_keys(
        interpolation,
        {
            "enabled_for_missing_target_only",
            "method",
            "same_business_date_only",
            "adjacent_valid_tenors_only",
            "cross_date_allowed",
            "extrapolation_allowed",
            "audit_fields",
        },
        "fx_policy.interpolation",
    )
    if not (
        raw["tenor_basis"] == "integer_month_difference"
        and raw["value_policy"] == "use_source_value_directly"
        and raw["distinguish_bid_ask_mid"] is False
        and raw["reject_negative_tenor"] is True
        and raw["reject_above_maximum_tenor"] is True
        and raw["exact_tenor_first"] is True
        and interpolation["enabled_for_missing_target_only"] is True
        and interpolation["method"] == "linear"
        and interpolation["same_business_date_only"] is True
        and interpolation["adjacent_valid_tenors_only"] is True
        and interpolation["cross_date_allowed"] is False
        and interpolation["extrapolation_allowed"] is False
    ):
        raise ImportProfitConfigError("fx_policy selection or interpolation rules are unsupported")
    return FxPolicy(
        minimum,
        maximum,
        tuple(sorted(columns.items())),
        True,
        "linear",
        True,
        True,
        False,
    )


def _parse_calendar_policy(value: object) -> BusinessCalendarPolicy:
    raw = _mapping(value, "business_calendar_policy")
    required = {
        "timezone",
        "calendar_type",
        "weekdays",
        "exclude_weekends",
        "exchange_holiday_calendar_required",
        "retain_weekday_without_market_data",
        "infer_exchange_open_from_market_data",
        "allow_previous_business_day_fallback",
        "missing_market_inputs",
        "record_specific_missing_reasons",
    }
    _require_exact_keys(raw, required, "business_calendar_policy")
    weekdays = tuple(_list(raw["weekdays"], "business_calendar_policy.weekdays"))
    if not (
        raw["timezone"] == "Asia/Shanghai"
        and raw["calendar_type"] == "weekday"
        and weekdays == ("monday", "tuesday", "wednesday", "thursday", "friday")
        and raw["exclude_weekends"] is True
        and raw["exchange_holiday_calendar_required"] is False
        and raw["retain_weekday_without_market_data"] is True
        and raw["infer_exchange_open_from_market_data"] is False
        and raw["allow_previous_business_day_fallback"] is False
        and raw["record_specific_missing_reasons"] is True
    ):
        raise ImportProfitConfigError("business calendar safety policy is unsupported")
    return BusinessCalendarPolicy(
        timezone="Asia/Shanghai",
        calendar_type="weekday",
        weekdays=weekdays,
        exclude_weekends=True,
        exchange_holiday_calendar_required=False,
        retain_weekday_without_market_data=True,
        infer_exchange_open_from_market_data=False,
        allow_previous_business_day_fallback=False,
    )


def _parse_display_policy(value: object) -> DisplayPolicy:
    raw = _mapping(value, "display_policy")
    required = {
        "application",
        "new_container",
        "add_home_core_card",
        "sidebar_group",
        "header_fields",
        "daily_table_columns",
        "editable_columns",
        "read_only_columns",
        "displayed_profit_metrics",
        "forbidden_profit_metrics",
        "recent_business_days",
        "recent_matrices",
    }
    _require_exact_keys(raw, required, "display_policy")
    displayed = tuple(_list(raw["displayed_profit_metrics"], "displayed_profit_metrics"))
    forbidden = tuple(_list(raw["forbidden_profit_metrics"], "forbidden_profit_metrics"))
    recent_days = _integer(raw["recent_business_days"], "recent_business_days")
    if not (
        raw["application"] == "spread-dashboard"
        and raw["new_container"] is False
        and raw["add_home_core_card"] is False
        and tuple(_list(raw["editable_columns"], "editable_columns")) == ("cnf",)
        and "cnf" not in set(_list(raw["read_only_columns"], "read_only_columns"))
        and displayed == ("screen_net_crush_margin",)
        and set(forbidden)
        == {
            "gross_crush_margin",
            "meal_cost",
            "oil_cost",
            "meal_breakeven_price",
            "oil_breakeven_price",
        }
        and recent_days == 10
    ):
        raise ImportProfitConfigError("display policy violates the soybean contract")
    return DisplayPolicy(str(raw["application"]), displayed, forbidden, recent_days)


def _parse_seasonality_policy(value: object) -> SeasonalityPolicy:
    raw = _mapping(value, "seasonality_policy")
    _require_exact_keys(raw, {"charts", "window", "displayed_years", "mean", "smoothing", "null_line_break"}, "seasonality_policy")
    window = _mapping(raw["window"], "seasonality_policy.window")
    years = _mapping(raw["displayed_years"], "seasonality_policy.displayed_years")
    mean = _mapping(raw["mean"], "seasonality_policy.mean")
    smoothing = _mapping(raw["smoothing"], "seasonality_policy.smoothing")
    if (
        window != {
            "start": "shipment_month_minus_4_month_start",
            "end": "shipment_month_minus_1_month_end",
        }
        or years.get("prior_shipment_years") != 5
        or years.get("prior_year_mean_years") != 5
        or years.get("current_shipment_year") is not True
        or mean.get("exclude_null") is not True
        or mean.get("minimum_valid_years") != 3
        or mean.get("insufficient_history_result") is not None
        or smoothing.get("enabled") is not False
        or raw["null_line_break"] is not True
    ):
        raise ImportProfitConfigError("seasonality policy violates the soybean contract")
    return SeasonalityPolicy(
        window_start=window["start"],
        window_end=window["end"],
        prior_shipment_years=5,
        prior_year_mean_years=5,
        minimum_valid_years=3,
        smoothing_enabled=False,
        null_line_break=True,
    )


def _parse_dce_daily_policy(value: object) -> DceDailyPolicy:
    source_policy = _mapping(value, "source_policy")
    dce = _mapping(source_policy.get("dce"), "source_policy.dce")
    incremental = _mapping(
        dce.get("incremental_source"),
        "source_policy.dce.incremental_source",
    )
    required = {
        "provider",
        "function",
        "source_field",
        "price_type",
        "price_semantics",
        "exchange",
        "capture_timezone",
        "scheduled_time",
        "capture_window",
        "source_quote_time_window",
        "trade_calendar_function",
        "require_same_business_date",
        "require_complete_contract_set",
        "allow_previous_date_fallback",
        "allow_post_close_fallback",
        "allow_historical_close_fallback",
        "allow_adjacent_contract_fallback",
        "allow_other_price_field_fallback",
        "defines_exchange_open_day",
    }
    _require_exact_keys(
        incremental,
        required,
        "source_policy.dce.incremental_source",
    )
    window = _mapping(
        incremental["capture_window"],
        "source_policy.dce.incremental_source.capture_window",
    )
    quote_window = _mapping(
        incremental["source_quote_time_window"],
        "source_policy.dce.incremental_source.source_quote_time_window",
    )
    _require_exact_keys(
        quote_window,
        {"start", "end_exclusive"},
        "source_policy.dce.incremental_source.source_quote_time_window",
    )
    _require_exact_keys(
        window,
        {"start", "end_exclusive"},
        "source_policy.dce.incremental_source.capture_window",
    )
    try:
        start = time.fromisoformat(str(window["start"]))
        end_exclusive = time.fromisoformat(str(window["end_exclusive"]))
        scheduled_time = time.fromisoformat(str(incremental["scheduled_time"]))
        quote_start = time.fromisoformat(str(quote_window["start"]))
        quote_end_exclusive = time.fromisoformat(str(quote_window["end_exclusive"]))
    except ValueError as exc:
        raise ImportProfitConfigError(
            "DCE capture window times must use ISO local time"
        ) from exc
    expected = {
        "provider": "akshare",
        "function": "futures_zh_spot",
        "source_field": "current_price",
        "price_type": "night_session_close",
        "price_semantics": "DCE_night_session_close",
        "exchange": "DCE",
        "capture_timezone": "Asia/Shanghai",
        "scheduled_time": "08:30:00",
        "trade_calendar_function": "tool_trade_date_hist_sina",
        "require_same_business_date": True,
        "require_complete_contract_set": False,
        "allow_previous_date_fallback": False,
        "allow_post_close_fallback": False,
        "allow_historical_close_fallback": False,
        "allow_adjacent_contract_fallback": False,
        "allow_other_price_field_fallback": False,
        "defines_exchange_open_day": False,
    }
    if (
        any(incremental[key] != expected_value for key, expected_value in expected.items())
        or scheduled_time != time(8, 30)
        or start != time(8, 30)
        or end_exclusive != time(8, 33)
        or quote_start != time(22, 59)
        or quote_end_exclusive != time(23, 1)
    ):
        raise ImportProfitConfigError(
            "DCE daily source policy violates the night-session-close contract"
        )
    return DEFAULT_DCE_DAILY_POLICY


def _mapping(value: object, path: str) -> Mapping[Any, Any]:
    if not isinstance(value, dict):
        raise ImportProfitConfigError(f"{path} must be a mapping")
    return value


def _list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ImportProfitConfigError(f"{path} must be a list")
    return value


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ImportProfitConfigError(f"{path} must be an integer")
    return value


def _month(value: object, path: str) -> int:
    result = _integer(value, path)
    if not 1 <= result <= 12:
        raise ImportProfitConfigError(f"{path} must be between 1 and 12")
    return result


def _require_exact_keys(value: Mapping[Any, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ImportProfitConfigError(f"{path} is missing keys: {sorted(missing)}")
    if unknown:
        raise ImportProfitConfigError(f"{path} contains unknown keys: {sorted(unknown)}")
