"""Pure presentation helpers for the soybean import-profit Streamlit page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite
from numbers import Real
from typing import Mapping, MutableMapping, Sequence

import pandas as pd
import plotly.graph_objects as go

from agri_research_agent.import_profit import (
    BusinessKey,
    SoybeanCalculationInput,
    SoybeanImportProfitConfig,
    SoybeanParameters,
    calculate_soybean_net_crush_margin,
    map_soybean_contracts,
)
from agri_research_agent.import_profit.historical_cnf_adapter import (
    shipment_year_for,
)
from agri_research_agent.import_profit.query import (
    QueryMetric,
    RecentMetricMatrix,
    SoybeanQueryDataset,
    SoybeanQueryRecord,
    build_recent_metric_matrix,
)
from agri_research_agent.import_profit.seasonality import (
    SeasonalityDataset,
    build_all_shipment_month_seasonality,
)


PAGE_STATE_PREFIX = "import_profit_page:"
STATE_CONTEXT = f"{PAGE_STATE_PREFIX}context"
STATE_ORIGINAL_CNF = f"{PAGE_STATE_PREFIX}original_cnf"
STATE_EDITED_CNF = f"{PAGE_STATE_PREFIX}edited_cnf"
STATE_PREVIEWS = f"{PAGE_STATE_PREFIX}previews"
STATE_LAST_LEGAL_EDIT = f"{PAGE_STATE_PREFIX}last_legal_edit"
STATE_EDITOR_VERSION = f"{PAGE_STATE_PREFIX}editor_version"
STATE_RUNTIME_CONTEXT = f"{PAGE_STATE_PREFIX}runtime_context"
STATE_SAVE_MESSAGE = f"{PAGE_STATE_PREFIX}save_message"

METRIC_SECTIONS = (
    (QueryMetric.CNF, "CNF报价", "CNF历史报价", "美分/蒲式耳"),
    (
        QueryMetric.DUTY_PAID_COST,
        "完税成本",
        "进口大豆完税成本",
        "元/吨",
    ),
    (
        QueryMetric.NET_CRUSH_MARGIN,
        "净榨利",
        "盘面净榨利",
        "元/吨",
    ),
)
PRICE_BASIS_NOTE = (
    "价格基准：外盘及汇率采用当日上午最新导入数据；"
    "内盘采用当交易日对应的夜盘收盘价，系统于北京时间08:30自动读取并冻结。"
)
HISTORICAL_PRICE_BASIS_NOTE = (
    "价格基准：当前历史记录保留原历史连续合约收盘口径，"
    "未改写为08:30夜盘收盘基准。"
)


class ImportProfitPageError(ValueError):
    """Base error for safe page preparation."""


class InvalidCnfPreviewError(ImportProfitPageError):
    """Raised when a CNF editor value is not a finite number or blank."""


class PreviewCompatibilityError(ImportProfitPageError):
    """Raised when a persisted record cannot safely reconstruct calculator input."""


@dataclass(frozen=True, slots=True)
class PageRuntimeIdentity:
    release_id: str
    generation: int
    previous_release_id: str | None
    index_sha256: str
    manual_cnf_sha256: str | None
    release_created_at: str
    manual_cnf_record_count: int


@dataclass(frozen=True, slots=True)
class RuntimeEditorContext:
    loaded_release_id: str
    loaded_generation: int
    loaded_index_sha256: str
    loaded_manual_cnf_sha256: str | None
    business_date: date
    origin: str
    business_keys: tuple[BusinessKey, ...]
    shipment_periods: tuple[str, ...]
    original_cnf: tuple[float | None, ...]
    original_sources: tuple[str | None, ...]


@dataclass(frozen=True, slots=True)
class CnfEditDifference:
    business_key: BusinessKey
    shipment_period: str
    previous_cnf: float | None
    new_cnf: float | None
    previous_source: str | None
    change_type: str


@dataclass(frozen=True, slots=True)
class RuntimeSaveFeedback:
    status: str
    message: str


@dataclass(frozen=True, slots=True)
class PreviewCalculation:
    shipment_period: str
    cnf_cents_per_bushel: float | None
    usd_cost_per_tonne: float | None
    duty_paid_cost_cny_per_tonne: float | None
    net_crush_margin_cny_per_tonne: float | None
    preview_status: str
    missing_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedPageData:
    records: tuple[SoybeanQueryRecord | None, ...]
    matrices: tuple[tuple[QueryMetric, RecentMetricMatrix], ...]
    seasonal: tuple[
        tuple[QueryMetric, tuple[SeasonalityDataset, ...]], ...
    ]

    def matrix(self, metric: QueryMetric) -> RecentMetricMatrix:
        return dict(self.matrices)[metric]

    def seasonality(
        self, metric: QueryMetric
    ) -> tuple[SeasonalityDataset, ...]:
        return dict(self.seasonal)[metric]


def origin_options(
    config: SoybeanImportProfitConfig,
) -> tuple[tuple[str, str], ...]:
    """Return the configured origin codes and labels in contract order."""

    return tuple((origin.code, origin.label) for origin in config.origins)


def origin_profit_title(origin: str, label: str) -> str:
    """Return the concise business title required by the page contract."""

    if origin == "brazil":
        return "巴西大豆榨利"
    if origin in {"us_gulf", "us_pnw"}:
        return "美国大豆榨利"
    if origin == "argentina":
        return "阿根廷大豆榨利"
    return f"{label}大豆榨利"


def price_basis_note(
    records: Sequence[SoybeanQueryRecord | None],
) -> str:
    """Describe persisted price types without relabelling historical rows."""

    price_types = {
        value
        for record in records
        if record is not None
        for value in (
            record.soymeal_price_type,
            record.soyoil_price_type,
        )
        if value
    }
    if price_types == {"historical_continuous_close"}:
        return HISTORICAL_PRICE_BASIS_NOTE
    return PRICE_BASIS_NOTE


def parameter_summary(
    config: SoybeanImportProfitConfig, origin: str
) -> str:
    """Render the selected origin's authoritative calculation parameters."""

    params = config.resolve_parameters(origin)
    return (
        f"出粕率{params.meal_yield:.1%}｜"
        f"出油率{params.oil_yield:.1%}｜"
        f"换算系数{params.cents_per_bushel_to_usd_per_tonne:.6f}｜"
        f"关税{params.tariff_rate:.0%}｜"
        f"增值税{params.vat_rate:.0%}｜"
        f"港杂费{_plain_number(params.port_charge_cny_per_tonne)}元/吨｜"
        f"加工费{_plain_number(params.processing_fee_cny_per_tonne)}元/吨"
    )


def records_for_date(
    dataset: SoybeanQueryDataset,
    *,
    origin: str,
    business_date: date,
) -> tuple[SoybeanQueryRecord | None, ...]:
    """Return fixed January-to-December slots without synthesizing records."""

    dataset.require_origin(origin)
    return tuple(
        dataset.get_by_date_origin_month(
            business_date, origin, shipment_month
        )
        for shipment_month in range(1, 13)
    )


def prepare_page_data(
    dataset: SoybeanQueryDataset,
    *,
    origin: str,
    business_date: date,
) -> PreparedPageData:
    """Prepare all formal page queries exactly once for one render."""

    records = records_for_date(
        dataset, origin=origin, business_date=business_date
    )
    matrices = tuple(
        (
            metric,
            build_recent_metric_matrix(
                dataset,
                origin=origin,
                as_of_date=business_date,
                metric=metric,
                lookback_days=10,
            ),
        )
        for metric, *_ in METRIC_SECTIONS
    )
    seasonal = tuple(
        (
            metric,
            build_all_shipment_month_seasonality(
                dataset,
                origin=origin,
                as_of_date=business_date,
                metric=metric,
            ),
        )
        for metric, *_ in METRIC_SECTIONS
    )
    return PreparedPageData(records, matrices, seasonal)


def date_status_counts(
    records: Sequence[SoybeanQueryRecord | None],
) -> tuple[int, int]:
    complete = sum(
        record is not None and record.snapshot_status == "complete"
        for record in records
    )
    return complete, len(records) - complete


def formal_daily_table(
    records: Sequence[SoybeanQueryRecord | None],
    *,
    business_date: date,
    origin: str,
    config: SoybeanImportProfitConfig,
) -> pd.DataFrame:
    """Build the read-only official-value table with explicit NULL markers."""

    if len(records) != 12:
        raise ImportProfitPageError("daily table requires twelve month slots")
    rows = []
    for month, record in enumerate(records, start=1):
        shipment_year = (
            record.shipment_year
            if record is not None
            else shipment_year_for(business_date, month)
        )
        params = config.resolve_parameters(origin)
        product_costs = _display_product_costs(record, params)
        rows.append(
            {
                "船期": (
                    record.shipment_period
                    if record is not None
                    else f"{shipment_year:04d}-{month:02d}"
                ),
                "CNF（美分/蒲）": _display_number(
                    record.cnf_cents_per_bushel if record else None
                ),
                "美元成本": _display_number(
                    record.usd_cost_per_tonne if record else None
                ),
                "CBOT合约": record.cbot_contract if record else "—",
                "CBOT价格": _display_number(
                    record.cbot_price_cents_per_bushel if record else None
                ),
                "汇率": _display_number(
                    record.fx_value if record else None, decimals=4
                ),
                "国内合约": _domestic_contract_label(record),
                "豆粕盘面": _display_number(
                    record.soymeal_price_cny_per_tonne if record else None
                ),
                "豆油盘面": _display_number(
                    record.soyoil_price_cny_per_tonne if record else None
                ),
                "关税%": f"{params.tariff_rate:.0%}",
                "增值税%": f"{params.vat_rate:.0%}",
                "完税成本": _display_number(
                    (
                        record.duty_paid_cost_cny_per_tonne
                        if record
                        else None
                    )
                ),
                "盘面榨利": _display_number(
                    (
                        record.net_crush_margin_cny_per_tonne
                        if record
                        else None
                    )
                ),
                "粕成本": product_costs[0],
                "油成本": product_costs[1],
            }
        )
    return pd.DataFrame(rows)


def _domestic_contract_label(
    record: SoybeanQueryRecord | None,
) -> str:
    """Combine M/Y contracts for display without changing stored contracts."""

    if record is None:
        return "—"
    soymeal = record.soymeal_contract.strip()
    soyoil = record.soyoil_contract.strip()
    meal_period = soymeal[1:] if soymeal[:1].upper() == "M" else soymeal
    oil_period = soyoil[1:] if soyoil[:1].upper() == "Y" else soyoil
    if meal_period and meal_period == oil_period:
        return meal_period
    if soymeal and soyoil:
        return f"{soymeal} / {soyoil}"
    return soymeal or soyoil or "—"


def _display_product_costs(
    record: SoybeanQueryRecord | None,
    params: SoybeanParameters,
) -> tuple[str, str]:
    """Derive the two residual product costs for presentation only."""

    if record is None or record.duty_paid_cost_cny_per_tonne is None:
        return "—", "—"
    total = (
        record.duty_paid_cost_cny_per_tonne
        + params.port_charge_cny_per_tonne
        + params.processing_fee_cny_per_tonne
        + params.additional_fees_cny_per_tonne
    )
    soymeal_value = None
    if record.soyoil_price_cny_per_tonne is not None:
        soymeal_value = (
            total
            - record.soyoil_price_cny_per_tonne * params.oil_yield
        ) / params.meal_yield
    soyoil_value = None
    if record.soymeal_price_cny_per_tonne is not None:
        soyoil_value = (
            total
            - record.soymeal_price_cny_per_tonne * params.meal_yield
        ) / params.oil_yield
    return _display_number(soymeal_value), _display_number(soyoil_value)


def editable_daily_table(
    records: Sequence[SoybeanQueryRecord | None],
    *,
    business_date: date,
    previews: Mapping[str, PreviewCalculation] | None = None,
) -> pd.DataFrame:
    """Build the editor table with formal and preview values separated."""

    if len(records) != 12:
        raise ImportProfitPageError("CNF editor requires twelve month slots")
    preview_values = previews or {}
    rows = []
    for month, record in enumerate(records, start=1):
        shipment_period = (
            record.shipment_period
            if record is not None
            else (
                f"{shipment_year_for(business_date, month):04d}-{month:02d}"
            )
        )
        preview = preview_values.get(shipment_period)
        rows.append(
            {
                "船期": shipment_period,
                "CNF升贴水": (
                    record.cnf_cents_per_bushel if record else None
                ),
                "CBOT合约": record.cbot_contract if record else None,
                "CBOT日度价格": (
                    record.cbot_price_cents_per_bushel if record else None
                ),
                "远期汇率": record.fx_value if record else None,
                "豆粕合约": record.soymeal_contract if record else None,
                "豆粕盘面": (
                    record.soymeal_price_cny_per_tonne if record else None
                ),
                "豆油合约": record.soyoil_contract if record else None,
                "豆油盘面": (
                    record.soyoil_price_cny_per_tonne if record else None
                ),
                "正式完税成本": (
                    record.duty_paid_cost_cny_per_tonne if record else None
                ),
                "正式盘面净榨利": (
                    record.net_crush_margin_cny_per_tonne
                    if record
                    else None
                ),
                "预览美元成本": (
                    preview.usd_cost_per_tonne if preview else None
                ),
                "预览完税成本": (
                    preview.duty_paid_cost_cny_per_tonne
                    if preview
                    else None
                ),
                "预览盘面净榨利": (
                    preview.net_crush_margin_cny_per_tonne
                    if preview
                    else None
                ),
                "预览状态": (
                    preview.preview_status if preview else "未预览"
                ),
                "剩余缺失原因": (
                    _display_reasons(preview.missing_reasons)
                    if preview
                    else "—"
                ),
            }
        )
    return pd.DataFrame(rows)


def parse_cnf_editor_value(value: object) -> float | None:
    """Normalize a CNF editor value while preserving zero and finite negatives."""

    if value is None or value is pd.NA:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        raise InvalidCnfPreviewError("CNF升贴水必须为有限数值或留空")
    if isinstance(value, bool) or not isinstance(value, Real):
        raise InvalidCnfPreviewError("CNF升贴水必须为有限数值或留空")
    parsed = float(value)
    if not isfinite(parsed):
        if pd.isna(parsed):
            return None
        raise InvalidCnfPreviewError("CNF升贴水不能为正负无穷")
    return parsed


def calculate_cnf_preview(
    record: SoybeanQueryRecord,
    edited_cnf: object,
    config: SoybeanImportProfitConfig,
) -> PreviewCalculation:
    """Replace only CNF and delegate all calculations to the existing kernel."""

    cnf_value = parse_cnf_editor_value(edited_cnf)
    mapped = map_soybean_contracts(
        config, record.shipment_year, record.shipment_month
    )
    if (
        record.cbot_contract != mapped.cbot.label
        or record.soymeal_contract != mapped.soymeal.code
        or record.soyoil_contract != mapped.soyoil.code
        or record.mapping_identity != mapped.mapping_identity
        or record.parameter_version != str(config.schema_version)
    ):
        raise PreviewCompatibilityError(
            "持久化合约、映射或参数身份与当前配置不一致"
        )
    key = BusinessKey(
        record.business_date,
        record.commodity,
        record.origin,
        record.shipment_year,
        record.shipment_month,
        config.origin_codes,
        config.commodity,
        record.shipment_period,
    )
    result = calculate_soybean_net_crush_margin(
        SoybeanCalculationInput(
            business_key=key,
            cnf_cents_per_bushel=cnf_value,
            cbot_contract=mapped.cbot,
            cbot_daily_price_cents_per_bushel=(
                record.cbot_price_cents_per_bushel
            ),
            fx_value=record.fx_value,
            soymeal_contract=mapped.soymeal,
            soymeal_price_cny_per_tonne=(
                record.soymeal_price_cny_per_tonne
            ),
            soyoil_contract=mapped.soyoil,
            soyoil_price_cny_per_tonne=record.soyoil_price_cny_per_tonne,
            resolved_parameters=config.resolve_parameters(record.origin),
            mapping_identity=record.mapping_identity,
        ),
        config,
    )
    return PreviewCalculation(
        shipment_period=record.shipment_period,
        cnf_cents_per_bushel=cnf_value,
        usd_cost_per_tonne=result.usd_cost_per_tonne,
        duty_paid_cost_cny_per_tonne=(
            result.duty_paid_cost_cny_per_tonne
        ),
        net_crush_margin_cny_per_tonne=(
            result.net_crush_margin_cny_per_tonne
        ),
        preview_status=result.calculation_status.value,
        missing_reasons=tuple(reason.value for reason in result.missing_reasons),
    )


def previews_from_editor(
    records: Sequence[SoybeanQueryRecord | None],
    edited: pd.DataFrame,
    config: SoybeanImportProfitConfig,
) -> dict[str, PreviewCalculation]:
    """Validate an editor payload and calculate only rows whose CNF changed."""

    if len(records) != 12 or len(edited.index) != 12:
        raise InvalidCnfPreviewError("CNF编辑表必须保持12个船期")
    if "船期" not in edited or "CNF升贴水" not in edited:
        raise InvalidCnfPreviewError("CNF编辑表缺少必要列")
    expected_periods = [
        record.shipment_period if record is not None else None
        for record in records
    ]
    previews: dict[str, PreviewCalculation] = {}
    for index, record in enumerate(records):
        period = str(edited.iloc[index]["船期"])
        expected = expected_periods[index]
        if record is None:
            continue
        if period != expected:
            raise InvalidCnfPreviewError("船期列不可修改")
        edited_value = parse_cnf_editor_value(
            edited.iloc[index]["CNF升贴水"]
        )
        if _same_optional_number(
            edited_value, record.cnf_cents_per_bushel
        ):
            continue
        previews[period] = calculate_cnf_preview(
            record, edited_value, config
        )
    return previews


def initialize_preview_state(
    state: MutableMapping[str, object],
    *,
    origin: str,
    business_date: date,
    records: Sequence[SoybeanQueryRecord | None],
    runtime_context: RuntimeEditorContext | None = None,
) -> None:
    """Reset page-local preview state whenever the selected context changes."""

    context = f"{origin}|{business_date.isoformat()}"
    if state.get(STATE_CONTEXT) == context:
        return
    originals = {
        record.shipment_period: record.cnf_cents_per_bushel
        for record in records
        if record is not None
    }
    state[STATE_CONTEXT] = context
    state[STATE_ORIGINAL_CNF] = originals
    state[STATE_EDITED_CNF] = dict(originals)
    state[STATE_PREVIEWS] = {}
    state[STATE_LAST_LEGAL_EDIT] = dict(originals)
    if runtime_context is None:
        state.pop(STATE_RUNTIME_CONTEXT, None)
    else:
        state[STATE_RUNTIME_CONTEXT] = runtime_context
    state[STATE_EDITOR_VERSION] = int(
        state.get(STATE_EDITOR_VERSION, 0)
    ) + 1


def restore_original_preview_state(
    state: MutableMapping[str, object],
) -> None:
    """Restore original CNF values without persisting anything."""

    originals = dict(state.get(STATE_ORIGINAL_CNF, {}))
    state[STATE_EDITED_CNF] = originals
    state[STATE_LAST_LEGAL_EDIT] = dict(originals)
    state[STATE_PREVIEWS] = {}
    state[STATE_EDITOR_VERSION] = int(
        state.get(STATE_EDITOR_VERSION, 0)
    ) + 1


def build_runtime_editor_context(
    records: Sequence[SoybeanQueryRecord | None],
    *,
    identity: PageRuntimeIdentity,
    config: SoybeanImportProfitConfig,
    origin: str,
    business_date: date,
) -> RuntimeEditorContext:
    """Freeze the exact Release and twelve full keys loaded by the editor."""

    if len(records) != 12 or any(record is None for record in records):
        raise ImportProfitPageError(
            "正式保存要求当前日期存在12个完整业务键"
        )
    actual = tuple(record for record in records if record is not None)
    keys = tuple(
        BusinessKey(
            record.business_date,
            record.commodity,
            record.origin,
            record.shipment_year,
            record.shipment_month,
            config.origin_codes,
            config.commodity,
            record.shipment_period,
        )
        for record in actual
    )
    return RuntimeEditorContext(
        loaded_release_id=identity.release_id,
        loaded_generation=identity.generation,
        loaded_index_sha256=identity.index_sha256,
        loaded_manual_cnf_sha256=identity.manual_cnf_sha256,
        business_date=business_date,
        origin=origin,
        business_keys=keys,
        shipment_periods=tuple(
            record.shipment_period for record in actual
        ),
        original_cnf=tuple(
            record.cnf_cents_per_bushel for record in actual
        ),
        original_sources=tuple(record.cnf_source for record in actual),
    )


def runtime_context_is_stale(
    context: RuntimeEditorContext,
    current: PageRuntimeIdentity,
) -> bool:
    return (
        context.loaded_release_id != current.release_id
        or context.loaded_generation != current.generation
        or context.loaded_index_sha256 != current.index_sha256
        or context.loaded_manual_cnf_sha256
        != current.manual_cnf_sha256
    )


def reset_runtime_editor_state(
    state: MutableMapping[str, object],
) -> None:
    """Drop only editor-local state so the next render loads Current."""

    for key in (
        STATE_CONTEXT,
        STATE_ORIGINAL_CNF,
        STATE_EDITED_CNF,
        STATE_PREVIEWS,
        STATE_LAST_LEGAL_EDIT,
        STATE_RUNTIME_CONTEXT,
    ):
        state.pop(key, None)
    state[STATE_EDITOR_VERSION] = int(
        state.get(STATE_EDITOR_VERSION, 0)
    ) + 1


def cnf_differences_from_editor(
    context: RuntimeEditorContext,
    edited: pd.DataFrame,
) -> tuple[CnfEditDifference, ...]:
    """Return only exact-key finite CNF changes from the loaded context."""

    if len(edited.index) != 12:
        raise InvalidCnfPreviewError("CNF编辑表必须保持12个船期")
    if "船期" not in edited or "CNF升贴水" not in edited:
        raise InvalidCnfPreviewError("CNF编辑表缺少必要列")
    differences: list[CnfEditDifference] = []
    for index, business_key in enumerate(context.business_keys):
        period = str(edited.iloc[index]["船期"])
        if period != context.shipment_periods[index]:
            raise InvalidCnfPreviewError("船期列不可修改")
        previous = context.original_cnf[index]
        new = parse_cnf_editor_value(
            edited.iloc[index]["CNF升贴水"]
        )
        if _same_optional_number(previous, new):
            continue
        source = context.original_sources[index]
        if new is None:
            change_type = "清空为NULL"
        elif previous is None:
            change_type = "新增人工覆盖"
        elif source == "manual_ui":
            change_type = "修改人工覆盖"
        else:
            change_type = "覆盖历史值"
        differences.append(
            CnfEditDifference(
                business_key=business_key,
                shipment_period=period,
                previous_cnf=previous,
                new_cnf=new,
                previous_source=source,
                change_type=change_type,
            )
        )
    return tuple(differences)


def matrix_display_frame(matrix: RecentMetricMatrix) -> pd.DataFrame:
    """Convert a matrix to its fixed 10×12 display representation."""

    rows = []
    for row in matrix.rows:
        values: dict[str, object] = {"日期": row.business_date.isoformat()}
        values.update(
            {
                f"{cell.shipment_month}月船期": _display_number(cell.value)
                for cell in row.cells
            }
        )
        rows.append(values)
    return pd.DataFrame(
        rows,
        columns=["日期", *(f"{month}月船期" for month in range(1, 13))],
    )


def seasonality_figure(
    seasonal: SeasonalityDataset,
    *,
    value_label: str,
) -> go.Figure:
    """Build one unsmoothed Plotly figure from prepared seasonality data."""

    figure = go.Figure()
    for index, series in enumerate(seasonal.series):
        current = index == 0
        customdata = [
            (
                point.business_date.isoformat(),
                point.season_year,
                point.shipment_period,
                point.status.value,
                _display_reasons(point.missing_reasons),
            )
            for point in series.points
        ]
        figure.add_trace(
            go.Scatter(
                x=[point.axis_date for point in series.points],
                y=[point.value for point in series.points],
                mode="lines",
                name=(
                    f"{series.season_year}年（当前）"
                    if current
                    else f"{series.season_year}年"
                ),
                line={
                    "width": 4 if current else 1.5,
                    "color": "#C1493F" if current else None,
                },
                connectgaps=False,
                customdata=customdata,
                hovertemplate=(
                    "真实日期：%{customdata[0]}<br>"
                    "船期年份：%{customdata[1]}<br>"
                    "船期：%{customdata[2]}<br>"
                    f"{value_label}：%{{y:.2f}}<br>"
                    "状态：%{customdata[3]}<br>"
                    "缺失原因：%{customdata[4]}<extra></extra>"
                ),
            )
        )
    mean_customdata = [
        (
            point.valid_sample_count,
            "、".join(str(year) for year in point.contributing_season_years)
            or "—",
        )
        for point in seasonal.five_year_mean
    ]
    figure.add_trace(
        go.Scatter(
            x=[point.axis_date for point in seasonal.five_year_mean],
            y=[point.value for point in seasonal.five_year_mean],
            mode="lines",
            name="前五年均值",
            line={"width": 2.5, "dash": "dash", "color": "#2F7D5A"},
            connectgaps=False,
            customdata=mean_customdata,
            hovertemplate=(
                f"{value_label}：%{{y:.2f}}<br>"
                "有效样本：%{customdata[0]}<br>"
                "贡献年份：%{customdata[1]}<extra></extra>"
            ),
        )
    )
    figure.update_layout(
        title=(
            f"{seasonal.shipment_month}月船期｜"
            f"{seasonal.current_shipment_year}年"
        ),
        height=360,
        margin={"l": 48, "r": 16, "t": 54, "b": 44},
        hovermode="closest",
        legend={
            "orientation": "h",
            "x": 0,
            "y": 1.02,
            "yanchor": "bottom",
            "font": {"size": 10},
        },
        xaxis={"tickformat": "%m-%d", "title": "观察窗口（月-日）"},
        yaxis={"title": value_label},
    )
    return figure


def seasonality_has_values(seasonal: SeasonalityDataset) -> bool:
    return any(
        point.value is not None
        for series in seasonal.series
        for point in series.points
    ) or any(
        point.value is not None for point in seasonal.five_year_mean
    )


def current_window_message(
    seasonal: SeasonalityDataset, as_of_date: date
) -> str | None:
    current = seasonal.series[0]
    if as_of_date < current.window_start:
        return "当前船期的观察窗口尚未开始。"
    current_points = [
        point
        for point in current.points
        if point.business_date <= as_of_date
    ]
    if current_points and not any(
        point.value is not None for point in current_points
    ):
        return "当前年份窗口内暂未形成连续报价。"
    return None


def _display_number(
    value: float | None, *, decimals: int = 2
) -> str:
    if value is None:
        return "—"
    if float(value) == 0:
        return "0"
    return f"{float(value):.{decimals}f}"


def _plain_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _fx_status(interpolated: bool) -> str:
    return "插值" if interpolated else "直接匹配"


def _display_reasons(reasons: Sequence[object]) -> str:
    return "、".join(str(reason) for reason in reasons) if reasons else "—"


def _same_optional_number(
    left: float | None, right: float | None
) -> bool:
    if left is None or right is None:
        return left is right
    return left == float(right)
