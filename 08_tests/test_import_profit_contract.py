"""Static contract checks for the imported commodity profit research framework."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "02_configs" / "import_profit_soybean.yaml"
CONTRACT_PATH = ROOT / "07_docs" / "projects" / "进口商品利润研究框架契约.md"
INDEX_PATH = ROOT / "07_docs" / "00_文档索引与适用范围.md"


def load_config() -> dict[str, Any]:
    assert CONFIG_PATH.is_file()
    loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_identity_scope_origins_and_parameters_are_locked() -> None:
    config = load_config()

    assert {
        "schema_version",
        "commodity",
        "page_title",
        "origins",
        "default_parameters",
        "origin_overrides",
        "cnf_unit",
        "stable_key",
        "fx_policy",
        "business_calendar_policy",
        "contract_mapping",
        "display_policy",
        "seasonality_policy",
        "source_policy",
    }.issubset(config)
    assert config["schema_version"]
    assert config["commodity"] == "soybean"
    assert config["page_title"] == "日度进口大豆盘面净榨利"
    assert config["framework"]["name"] == "进口商品利润研究框架"
    assert config["framework"]["python_package"] == "import_profit"
    assert config["framework"]["shared_kernel"] == "thin"
    assert config["framework"]["product_models"] == {
        "soybean": "crush_profit",
        "rapeseed": "crush_profit",
        "palm_oil": "direct_import_profit",
    }
    assert config["framework"]["palm_oil_uses_crush_yields"] is False
    assert [origin["label"] for origin in config["origins"]] == [
        "巴西",
        "美湾",
        "美西",
        "阿根廷",
    ]

    parameters = config["default_parameters"]
    assert parameters["meal_yield"] == 0.795
    assert parameters["oil_yield"] == 0.19
    assert parameters["cents_per_bushel_to_usd_per_tonne"] == 0.367437
    assert parameters["tariff_rate"] == 0.03
    assert parameters["vat_rate"] == 0.09
    assert parameters["port_charge_cny_per_tonne"] == 50
    assert parameters["processing_fee_cny_per_tonne"] == 150
    assert parameters["additional_fees_cny_per_tonne"] == 0
    assert config["origin_overrides"] == {}


def test_cnf_key_unit_overwrite_and_null_rules_are_explicit() -> None:
    config = load_config()

    assert config["cnf_unit"] == "cents_per_bushel"
    assert config["stable_key"] == [
        "business_date",
        "commodity",
        "origin",
        "shipment_year",
        "shipment_month",
    ]
    assert config["shipment_period"] == {
        "format": "YYYY-MM",
        "replaces_stable_key_validation": False,
    }

    policy = config["cnf_policy"]
    assert policy["same_key_save"] == "overwrite"
    assert policy["historical_entry_allowed"] is True
    assert policy["historical_edit_allowed"] is True
    assert policy["keep_revision_history"] is False
    assert policy["null_is_missing"] is True
    assert policy["zero_is_valid"] is True
    for forbidden_fill in (
        "fill_forward",
        "fill_backward",
        "inherit_across_dates",
        "interpolate",
        "substitute_adjacent_shipment",
    ):
        assert policy[forbidden_fill] is False
    assert policy["null_downstream_fields"] == [
        "usd_cost",
        "landed_duty_paid_cost",
        "screen_net_crush_margin",
    ]


def test_fx_policy_covers_exact_tenors_and_only_same_day_adjacent_interpolation() -> None:
    fx_policy = load_config()["fx_policy"]

    assert fx_policy["source_table"] == "美元兑人民币历史汇率"
    assert fx_policy["value_policy"] == "use_source_value_directly"
    assert fx_policy["distinguish_bid_ask_mid"] is False
    assert fx_policy["tenor_basis"] == "integer_month_difference"
    assert fx_policy["minimum_tenor_months"] == 0
    assert fx_policy["maximum_tenor_months"] == 12
    assert fx_policy["reject_negative_tenor"] is True
    assert fx_policy["reject_above_maximum_tenor"] is True
    assert fx_policy["tenor_columns"] == {
        0: "Spot",
        1: "Fwd_1M",
        2: "Fwd_2M",
        3: "Fwd_3M",
        4: "Fwd_4M",
        5: "Fwd_5M",
        6: "Fwd_6M",
        7: "Fwd_7M",
        8: "Fwd_8M",
        9: "Fwd_9M",
        10: "Fwd_10M",
        11: "Fwd_11M",
        12: "Fwd_1Y",
    }
    assert fx_policy["exact_tenor_first"] is True
    interpolation = fx_policy["interpolation"]
    assert interpolation["enabled_for_missing_target_only"] is True
    assert interpolation["method"] == "linear"
    assert interpolation["same_business_date_only"] is True
    assert interpolation["adjacent_valid_tenors_only"] is True
    assert interpolation["cross_date_allowed"] is False
    assert interpolation["extrapolation_allowed"] is False
    assert interpolation["audit_fields"] == [
        "is_interpolated",
        "lower_tenor",
        "upper_tenor",
    ]


EXPECTED_MAPPING = [
    (1, 1, 0, 5, 0),
    (2, 3, 0, 5, 0),
    (3, 3, 0, 5, 0),
    (4, 5, 0, 5, 0),
    (5, 5, 0, 5, 0),
    (6, 7, 0, 5, 0),
    (7, 7, 0, 5, 0),
    (8, 9, 0, 5, 0),
    (9, 9, 0, 1, 1),
    (10, 11, 0, 1, 1),
    (11, 11, 0, 1, 1),
    (12, 1, 1, 1, 1),
]


def test_all_twelve_shipment_month_mappings_are_complete_unique_and_shared() -> None:
    mapping = load_config()["contract_mapping"]
    rows = mapping["rows"]

    actual = [
        (
            row["shipment_month"],
            row["cbot"]["contract_month"],
            row["cbot"]["year_offset"],
            row["dce"]["contract_month"],
            row["dce"]["year_offset"],
        )
        for row in rows
    ]
    assert actual == EXPECTED_MAPPING
    assert len({row["shipment_month"] for row in rows}) == 12
    assert mapping["contract_year_expression"] == "shipment_year + year_offset"
    assert mapping["code_policy"] == "month_and_year_offset_only"
    assert set(mapping["shared_across"]) == {
        "page",
        "historical_initialization",
        "historical_recalculation",
        "recent_table",
        "seasonality_chart",
    }


def test_business_calendar_status_and_scheduler_boundaries_are_locked() -> None:
    calendar = load_config()["business_calendar_policy"]

    assert calendar["timezone"] == "Asia/Shanghai"
    assert calendar["calendar_type"] == "weekday"
    assert calendar["weekdays"] == [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
    ]
    assert calendar["exclude_weekends"] is True
    assert calendar["exchange_holiday_calendar_required"] is False
    assert calendar["retain_weekday_without_market_data"] is True
    assert calendar["infer_exchange_open_from_market_data"] is False
    assert calendar["allow_previous_business_day_fallback"] is False
    assert calendar["record_specific_missing_reasons"] is True


def test_display_and_seasonality_rules_only_expose_net_margin() -> None:
    config = load_config()
    display = config["display_policy"]
    seasonality = config["seasonality_policy"]

    assert display["application"] == "spread-dashboard"
    assert display["new_container"] is False
    assert display["add_home_core_card"] is False
    assert display["sidebar_group"] == "市场行情"
    assert display["editable_columns"] == ["cnf"]
    assert display["displayed_profit_metrics"] == ["screen_net_crush_margin"]
    assert set(display["forbidden_profit_metrics"]) == {
        "gross_crush_margin",
        "meal_cost",
        "oil_cost",
        "meal_breakeven_price",
        "oil_breakeven_price",
    }
    assert display["recent_business_days"] == 10
    assert display["recent_matrices"] == [
        "cnf",
        "landed_duty_paid_cost",
        "screen_net_crush_margin",
    ]

    assert seasonality["window"] == {
        "start": "shipment_month_minus_4_month_start",
        "end": "shipment_month_minus_1_month_end",
    }
    assert seasonality["displayed_years"]["prior_shipment_years"] == 5
    assert seasonality["displayed_years"]["prior_year_mean_years"] == 5
    assert seasonality["mean"]["exclude_null"] is True
    assert seasonality["mean"]["minimum_valid_years"] == 3
    assert seasonality["mean"]["insufficient_history_result"] is None
    assert seasonality["smoothing"]["enabled"] is False
    assert seasonality["null_line_break"] is True


def test_sources_are_read_only_and_update_flows_remain_separate() -> None:
    source = load_config()["source_policy"]

    assert source["cbot"]["display_name"] == "CBOT日度价格"
    assert source["cbot"]["forbidden_names"] == ["CBOT收盘价", "CBOT结算价"]
    assert source["dce"]["historical_source"] == {
        "kind": "existing_database",
        "purpose": ["initialization", "historical_recalculation"],
        "preserve_source_and_price_type": True,
    }
    incremental = source["dce"]["incremental_source"]
    assert incremental == {
        "provider": "akshare",
        "function": "futures_zh_spot",
        "source_field": "current_price",
        "price_type": "night_session_close",
        "price_semantics": "DCE_night_session_close",
        "exchange": "DCE",
        "capture_timezone": "Asia/Shanghai",
        "scheduled_time": "08:30:00",
        "capture_window": {
            "start": "08:30:00",
            "end_exclusive": "08:33:00",
        },
        "source_quote_time_window": {
            "start": "22:59:00",
            "end_exclusive": "23:01:00",
        },
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
    assert source["raw_inputs"]["excel"]["writable_by_program"] is False
    assert source["raw_inputs"]["sql"]["writable_by_program"] is False
    assert source["daily_sql_committed_to_git"] is False
    assert source["code_release_separate_from_daily_sql_update"] is True
    assert source["sql_update"] == {
        "candidate_validation_required": True,
        "stable_slots": ["current", "previous"],
        "atomic_promotion_required": True,
    }
    assert all(source["dce_daily_update"].values())
    assert source["production_scheduler_in_scope"] is False


def iter_keys_and_values(value: Any, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield path + (str(key),), child
            yield from iter_keys_and_values(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_keys_and_values(child, path + (str(index),))


def test_config_contains_no_local_absolute_path_or_server_secret() -> None:
    config = load_config()
    rendered = CONFIG_PATH.read_text(encoding="utf-8")

    assert not re.search(r"(?im)(?:^|[\s\"'])[A-Z]:[\\/]", rendered)
    assert "/home/" not in rendered
    assert "server_address" not in rendered

    forbidden_secret_keys = {
        "password",
        "passwd",
        "token",
        "api_key",
        "secret",
        "private_key",
    }
    for path, _value in iter_keys_and_values(config):
        assert path[-1].lower() not in forbidden_secret_keys


def test_unique_contract_is_registered_and_contains_required_business_rules() -> None:
    assert CONTRACT_PATH.is_file()
    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    index = INDEX_PATH.read_text(encoding="utf-8")

    assert index.count("projects/进口商品利润研究框架契约.md") == 2
    assert "当前项目唯一权威业务契约" in contract
    for required in (
        "进口商品利润研究框架",
        "日度进口大豆盘面净榨利",
        "棕榈油使用直接进口利润模型",
        "棕榈油使用直接进口利润模型，不得套用出粕率或出油率",
        "美元成本 = (CBOT日度价格 + CNF升贴水) × 0.367437",
        "完税成本 = 美元成本 × 汇率 × (1 + 关税率) × (1 + 增值税率)",
        "盘面净榨利 = 豆粕盘面 × 0.795",
        "数字 `0` 是有效平水报价",
        "同一 `business_date` 的相邻有效期限",
        "不得依据 AkShare 是否返回行情推断交易所开市或休市",
        "`Asia/Shanghai` 时区下的周一至周五",
        "中国法定节假日即使落在周一至周五也保留历史日期行",
        "可审计交易日历确认目标业务日期",
        "不得使用上一业务日行情回填",
        "Public Current",
        "compare-and-swap",
        "application-service credential",
        "import-profit/operational/cnf/cnf.sqlite3",
        "manual_cnf_quotes.parquet",
        "相邻真实有效报价间隔不超过 10 个日历日时连线",
        "未修改键的原历史净榨利保持不变",
        "不展示 CNF 报价图、到港完税成本图",
        "不得回退到前一日期",
        "录入、预览或保存 CNF 时不得重新获取行情",
        "全量更新暂不可用",
        "页面不连接供应商数据库、不采集、不建立服务器定时任务",
        "每日 SQL 数据及其运行结果不得提交 Git",
    ):
        assert required in contract


def test_contract_and_config_remove_obsolete_incremental_price_wording() -> None:
    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    config = CONFIG_PATH.read_text(encoding="utf-8")
    combined = contract + config
    for obsolete in (
        "post_close_current_price",
        "DCE 日盘收盘后最后价",
        "15:05:00",
        "15:20:00",
    ):
        assert obsolete not in combined
