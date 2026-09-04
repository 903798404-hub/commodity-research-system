"""Streamlit page for daily imported-soybean screen net crush margin."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import Callable, Mapping

import pandas as pd
import streamlit as st

from agri_research_agent.import_profit import (
    ImportProfitConfigError,
    SoybeanImportProfitConfig,
    load_soybean_config,
)
from agri_research_agent.import_profit.query import (
    ImportProfitQueryError,
    QueryMetric,
    SoybeanQueryDataset,
    load_soybean_query_dataset,
)
from agri_research_agent.import_profit.parameter_snapshot import (
    ParameterProvenance,
)
from agri_research_agent.import_profit.scenario import (
    SCENARIO_CONTEXT_STATE,
    SCENARIO_TARIFF_STATE,
    SCENARIO_VAT_STATE,
    ScenarioError,
    ScenarioRates,
    calculate_scenario_batch,
    initialize_scenario_state,
    reset_scenario_state,
)
from import_profit_components import (
    METRIC_SECTIONS,
    STATE_EDITED_CNF,
    STATE_EDITOR_VERSION,
    STATE_LAST_LEGAL_EDIT,
    STATE_PREVIEWS,
    STATE_RUNTIME_CONTEXT,
    STATE_SAVE_MESSAGE,
    CnfEditDifference,
    ImportProfitPageError,
    InvalidCnfPreviewError,
    PageRuntimeIdentity,
    PreparedPageData,
    PreviewCalculation,
    RuntimeEditorContext,
    RuntimeSaveFeedback,
    build_runtime_editor_context,
    cnf_differences_from_editor,
    current_window_message,
    date_status_counts,
    editable_daily_table,
    formal_daily_table,
    initialize_preview_state,
    matrix_display_frame,
    origin_options,
    origin_profit_title,
    price_basis_note,
    parameter_summary,
    prepare_page_data,
    previews_from_editor,
    restore_original_preview_state,
    reset_runtime_editor_state,
    runtime_context_is_stale,
    seasonality_figure,
    seasonality_has_values,
)
from ui_theme import inject_workspace_theme, render_section_heading


PAGE_TITLE = "日度进口大豆盘面净榨利"
PAGE_DESCRIPTION = (
    "基于CNF、CBOT日度价格、远期汇率以及豆粕、豆油盘面计算；"
    "仅展示扣除港杂费和加工费后的盘面净榨利。"
)
MISSING_DATA_EXPLANATION = (
    "无CNF或必要行情时，相关计算结果保持为空。"
)
PREVIEW_NOTICE = (
    "预览模式：当前CNF修改仅用于本次页面计算，尚未写入正式数据。"
)
RUNTIME_NOTICE = (
    "正式运行模式：当前页面读取的是正式运行Release；"
    "保存操作会建立新的不可变Release。"
)
SCENARIO_TARIFF_PERCENT_STATE = (
    "import_profit_page:scenario_tariff_percent"
)
SCENARIO_VAT_PERCENT_STATE = "import_profit_page:scenario_vat_percent"

RuntimeSaveHandler = Callable[
    [RuntimeEditorContext, tuple[CnfEditDifference, ...]],
    RuntimeSaveFeedback,
]


@dataclass(frozen=True, slots=True)
class ImportProfitPageDataPaths:
    business_keys_path: Path
    snapshots_path: Path
    results_path: Path

    def __post_init__(self) -> None:
        for field_name in (
            "business_keys_path",
            "snapshots_path",
            "results_path",
        ):
            object.__setattr__(
                self, field_name, Path(getattr(self, field_name))
            )

    @property
    def paths(self) -> tuple[Path, Path, Path]:
        return (
            self.business_keys_path,
            self.snapshots_path,
            self.results_path,
        )


@st.cache_resource(show_spinner=False)
def _load_dataset_cached(
    business_keys_path: str,
    business_keys_mtime_ns: int,
    business_keys_size: int,
    snapshots_path: str,
    snapshots_mtime_ns: int,
    snapshots_size: int,
    results_path: str,
    results_mtime_ns: int,
    results_size: int,
) -> SoybeanQueryDataset:
    """Load once per exact path/mtime/size identity."""

    del (
        business_keys_mtime_ns,
        business_keys_size,
        snapshots_mtime_ns,
        snapshots_size,
        results_mtime_ns,
        results_size,
    )
    return load_soybean_query_dataset(
        business_keys_path, snapshots_path, results_path
    )


@st.cache_resource(show_spinner=False)
def _load_config_cached(
    config_path: str, config_mtime_ns: int, config_size: int
) -> SoybeanImportProfitConfig:
    del config_mtime_ns, config_size
    return load_soybean_config(config_path)


def render_import_profit_page(
    data_paths: ImportProfitPageDataPaths,
    *,
    config_path: str | Path,
    allow_cnf_preview: bool = True,
) -> None:
    """Load injected standard files and render the complete isolated page."""

    inject_workspace_theme()
    _render_page_intro()
    try:
        signatures = tuple(_file_signature(path) for path in data_paths.paths)
        config_signature = _file_signature(Path(config_path))
        with st.spinner("正在读取进口利润历史候选，请稍等..."):
            dataset = _load_dataset_cached(
                signatures[0][0],
                signatures[0][1],
                signatures[0][2],
                signatures[1][0],
                signatures[1][1],
                signatures[1][2],
                signatures[2][0],
                signatures[2][1],
                signatures[2][2],
            )
            config = _load_config_cached(*config_signature)
    except FileNotFoundError as exc:
        st.error(_safe_missing_file_message(exc))
        return
    except (ImportProfitQueryError, ImportProfitConfigError):
        st.error(
            "历史候选或配置的结构校验失败，请检查标准文件后重试。"
        )
        return
    except OSError:
        st.error("标准候选文件暂时无法读取，请稍后重试。")
        return
    render_import_profit_page_from_dataset(
        dataset,
        config=config,
        allow_cnf_preview=allow_cnf_preview,
        intro_rendered=True,
    )


def render_import_profit_page_from_dataset(
    dataset: SoybeanQueryDataset,
    *,
    config: SoybeanImportProfitConfig,
    allow_cnf_preview: bool = True,
    intro_rendered: bool = False,
    runtime_identity: PageRuntimeIdentity | None = None,
    allow_cnf_save: bool = False,
    runtime_save_handler: RuntimeSaveHandler | None = None,
    release_parameters_available: bool = True,
    release_mapping_available: bool = True,
    release_contract_override_available: bool = True,
    parameter_provenance: ParameterProvenance | None = None,
) -> None:
    """Render from an injected immutable dataset for tests and future routing."""

    if not intro_rendered:
        inject_workspace_theme()
        _render_page_intro(runtime_identity)
    if dataset.business_key_count == 0 or dataset.date_range == (None, None):
        st.warning("历史候选当前为空，暂无可展示的业务记录。")
        return
    if not set(dataset.origins).issubset(config.origin_codes):
        st.error("历史候选包含配置之外的产地代码。")
        return

    available_origins = tuple(
        (code, label)
        for code, label in origin_options(config)
        if code in dataset.origins
    )
    if not available_origins:
        st.warning("当前候选没有可用产地。")
        return
    labels = dict(available_origins)
    start_date, end_date = dataset.date_range
    assert start_date is not None and end_date is not None

    control_columns = st.columns((1.1, 1.1, 2.8))
    with control_columns[0]:
        selected_origin = st.selectbox(
            "产地",
            options=[code for code, _ in available_origins],
            format_func=labels.__getitem__,
            key="import_profit_page:origin_selector",
        )
    with control_columns[1]:
        selected_date = st.date_input(
            "业务日期",
            value=end_date,
            min_value=start_date,
            max_value=end_date,
            key="import_profit_page:business_date_selector",
        )
    with control_columns[2]:
        st.caption("计算参数摘要")
        st.markdown(
            parameter_summary(
                config,
                selected_origin,
                parameters_available=release_parameters_available,
            )
        )

    if not isinstance(selected_date, date):
        st.error("业务日期选择无效。")
        return
    if selected_date.weekday() >= 5:
        st.error("业务日期必须为周一至周五，页面不会自动回退日期。")
        return

    try:
        preparation_started = perf_counter()
        prepared = prepare_page_data(
            dataset,
            origin=selected_origin,
            business_date=selected_date,
        )
        preparation_seconds = perf_counter() - preparation_started
    except ImportProfitPageError:
        st.error("页面数据准备失败，请检查查询结果结构。")
        return
    except ImportProfitQueryError:
        st.error("页面查询条件与历史候选不一致。")
        return

    runtime_context = None
    runtime_context_error = None
    if runtime_identity is not None:
        try:
            runtime_context = build_runtime_editor_context(
                prepared.records,
                identity=runtime_identity,
                config=config,
                origin=selected_origin,
                business_date=selected_date,
            )
        except ImportProfitPageError as exc:
            runtime_context_error = str(exc)
    initialize_preview_state(
        st.session_state,
        origin=selected_origin,
        business_date=selected_date,
        records=prepared.records,
        runtime_context=runtime_context,
    )
    _render_data_status(
        dataset,
        prepared,
        origin_label=labels[selected_origin],
        business_date=selected_date,
        runtime_identity=runtime_identity,
    )
    save_message = st.session_state.pop(STATE_SAVE_MESSAGE, None)
    if isinstance(save_message, str) and save_message:
        st.success(save_message)
    st.caption(f"本次页面数据准备耗时：{preparation_seconds:.3f}秒")
    _render_daily_table(
        prepared,
        config=config,
        origin=selected_origin,
        origin_label=labels[selected_origin],
        business_date=selected_date,
        parameters_available=release_parameters_available,
    )
    _render_contract_override_notice(prepared.records)
    if runtime_identity is not None:
        _render_tariff_vat_scenario(
            dataset,
            prepared,
            config=config,
            provenance=parameter_provenance,
            origin=selected_origin,
            context_id=runtime_identity.release_id,
            mapping_available=release_mapping_available,
            contract_override_available=(
                release_contract_override_available
            ),
        )
    if (
        allow_cnf_preview
        and release_parameters_available
        and release_mapping_available
        and release_contract_override_available
    ):
        _render_cnf_editor(
            prepared,
            config=config,
            business_date=selected_date,
            runtime_identity=runtime_identity,
            allow_cnf_save=allow_cnf_save,
            runtime_save_handler=runtime_save_handler,
            runtime_context_error=runtime_context_error,
        )
    _render_recent_matrices(
        prepared,
        origin_label=labels[selected_origin],
        business_date=selected_date,
    )
    _render_seasonality(
        prepared,
        business_date=selected_date,
    )


def _render_tariff_vat_scenario(
    dataset: SoybeanQueryDataset,
    prepared: PreparedPageData,
    *,
    config: SoybeanImportProfitConfig,
    provenance: ParameterProvenance | None,
    origin: str,
    context_id: str,
    mapping_available: bool,
    contract_override_available: bool,
) -> None:
    render_section_heading(
        "SCENARIO｜关税 / VAT 情景试算",
        "仅改变当前Release的关税率和增值税率",
    )
    st.warning(
        "SESSION-ONLY / NO WRITES｜仅页面内存："
        "不写入Release、Runtime、CNF、YAML或Parquet，"
        "Official结果始终保持不变。"
    )
    if not mapping_available:
        st.info(
            "Official mapping snapshot unavailable；"
            "历史Release无可信换月规则快照，Scenario已禁用。"
        )
        return
    if not contract_override_available:
        st.info(
            "Official contract override snapshot unavailable；"
            "历史Release无可信人工合约覆盖信息，Scenario已禁用。"
        )
        return
    if provenance is None or not provenance.available:
        st.info(
            "Official parameter snapshot unavailable；"
            "历史Release无可信参数快照，Scenario已禁用。"
        )
        return
    previous_context = st.session_state.get(SCENARIO_CONTEXT_STATE)
    try:
        official = initialize_scenario_state(
            st.session_state,
            context_id=context_id,
            origin=origin,
            provenance=provenance,
        )
    except ScenarioError:
        st.info("Official parameter snapshot unavailable；Scenario已禁用。")
        return
    current_context = st.session_state.get(SCENARIO_CONTEXT_STATE)
    if previous_context != current_context:
        st.session_state[SCENARIO_TARIFF_PERCENT_STATE] = (
            official.tariff_rate * 100.0
        )
        st.session_state[SCENARIO_VAT_PERCENT_STATE] = (
            official.vat_rate * 100.0
        )
    controls = st.columns((1, 1, 1, 3))
    tariff_percent = controls[0].number_input(
        "Scenario关税（%）",
        min_value=0.0,
        max_value=100.0,
        step=0.1,
        key=SCENARIO_TARIFF_PERCENT_STATE,
    )
    vat_percent = controls[1].number_input(
        "Scenario VAT（%）",
        min_value=0.0,
        max_value=100.0,
        step=0.1,
        key=SCENARIO_VAT_PERCENT_STATE,
    )
    def reset_to_official() -> None:
        reset = reset_scenario_state(
            st.session_state,
            origin=origin,
            provenance=provenance,
        )
        st.session_state[SCENARIO_TARIFF_PERCENT_STATE] = (
            reset.tariff_rate * 100.0
        )
        st.session_state[SCENARIO_VAT_PERCENT_STATE] = reset.vat_rate * 100.0

    controls[2].button(
        "Reset to Official",
        key="import_profit_page:scenario_reset",
        on_click=reset_to_official,
    )
    rates = ScenarioRates(tariff_percent / 100.0, vat_percent / 100.0)
    st.session_state[SCENARIO_TARIFF_STATE] = rates.tariff_rate
    st.session_state[SCENARIO_VAT_STATE] = rates.vat_rate
    metrics = st.columns(4)
    metrics[0].metric("Official Tariff", f"{official.tariff_rate:.2%}")
    metrics[1].metric("Scenario Tariff", f"{rates.tariff_rate:.2%}")
    metrics[2].metric("Official VAT", f"{official.vat_rate:.2%}")
    metrics[3].metric("Scenario VAT", f"{rates.vat_rate:.2%}")
    try:
        current = calculate_scenario_batch(
            (record for record in prepared.records if record is not None),
            config=config,
            provenance=provenance,
            rates=rates,
        )
        st.dataframe(
            _scenario_current_frame(current),
            hide_index=True,
            width="stretch",
            height=460,
        )
        margin_frame, impact_frame = _scenario_recent_frames(
            dataset,
            prepared,
            config=config,
            provenance=provenance,
            origin=origin,
            rates=rates,
        )
    except ScenarioError:
        st.error("Scenario计算与当前Release记录不兼容，未展示试算。")
        return
    st.caption("Scenario Net Crush Margin｜近10个业务日 × 12船期")
    st.dataframe(margin_frame, hide_index=True, width="stretch")
    st.caption("Scenario Impact (CNY/t)｜Scenario - Official")
    st.dataframe(impact_frame, hide_index=True, width="stretch")


def _scenario_current_frame(calculations) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "船期": item.shipment_period,
                "Official Tariff (%)": item.official_tariff_rate * 100.0,
                "Scenario Tariff (%)": item.scenario_tariff_rate * 100.0,
                "Official VAT (%)": item.official_vat_rate * 100.0,
                "Scenario VAT (%)": item.scenario_vat_rate * 100.0,
                "Official Net Crush Margin (CNY/t)": (
                    item.official_net_crush_margin_cny_per_tonne
                ),
                "Scenario Net Crush Margin (CNY/t)": (
                    item.scenario_net_crush_margin_cny_per_tonne
                ),
                "Scenario Impact (CNY/t)": item.scenario_impact_cny_per_tonne,
            }
            for item in calculations
        ]
    )


def _scenario_recent_frames(
    dataset,
    prepared,
    *,
    config,
    provenance,
    origin,
    rates,
):
    margin_rows = []
    impact_rows = []
    for recent_row in prepared.matrix(QueryMetric.NET_CRUSH_MARGIN).rows:
        records = tuple(
            record
            for month in range(1, 13)
            if (
                record := dataset.get_by_date_origin_month(
                    recent_row.business_date, origin, month
                )
            )
            is not None
        )
        calculated = calculate_scenario_batch(
            records,
            config=config,
            provenance=provenance,
            rates=rates,
        )
        by_month = {
            item.business_key.shipment_month: item for item in calculated
        }
        margin = {"日期": recent_row.business_date}
        impact = {"日期": recent_row.business_date}
        for month in range(1, 13):
            item = by_month.get(month)
            margin[f"{month}月船期"] = (
                None
                if item is None
                else item.scenario_net_crush_margin_cny_per_tonne
            )
            impact[f"{month}月船期"] = (
                None if item is None else item.scenario_impact_cny_per_tonne
            )
        margin_rows.append(margin)
        impact_rows.append(impact)
    columns = ["日期", *(f"{month}月船期" for month in range(1, 13))]
    return (
        pd.DataFrame(margin_rows, columns=columns),
        pd.DataFrame(impact_rows, columns=columns),
    )


def _render_page_intro(
    runtime_identity: PageRuntimeIdentity | None = None,
) -> None:
    st.title(PAGE_TITLE)
    st.markdown(PAGE_DESCRIPTION)
    st.caption(MISSING_DATA_EXPLANATION)
    st.info(RUNTIME_NOTICE if runtime_identity is not None else PREVIEW_NOTICE)


def _render_data_status(
    dataset: SoybeanQueryDataset,
    prepared: PreparedPageData,
    *,
    origin_label: str,
    business_date: date,
    runtime_identity: PageRuntimeIdentity | None = None,
) -> None:
    render_section_heading(
        "数据状态",
        (
            "正式运行Release"
            if runtime_identity is not None
            else "预览候选的只读统计"
        ),
    )
    complete, incomplete = date_status_counts(prepared.records)
    start_date, end_date = dataset.date_range
    columns = st.columns(5)
    columns[0].metric(
        "数据日期范围", f"{start_date} 至 {end_date}"
    )
    columns[1].metric("总记录数", f"{dataset.business_key_count:,}")
    columns[2].metric("成功计算数", f"{dataset.success_count:,}")
    columns[3].metric("不完整记录数", f"{dataset.incomplete_count:,}")
    columns[4].metric("当前产地/日期", f"{origin_label}｜{business_date}")
    st.caption(
        f"当前日期完整船期 {complete} 个｜不完整船期 {incomplete} 个"
    )
    if runtime_identity is not None:
        identity_columns = st.columns(5)
        identity_columns[0].metric(
            "当前Release", runtime_identity.release_id
        )
        identity_columns[1].metric(
            "generation", runtime_identity.generation
        )
        identity_columns[2].metric(
            "Previous Release",
            runtime_identity.previous_release_id or "—",
        )
        identity_columns[3].metric(
            "Release创建时间", runtime_identity.release_created_at
        )
        identity_columns[4].metric(
            "人工CNF记录数",
            runtime_identity.manual_cnf_record_count,
        )
        st.caption(
            "当前页面读取身份：Release、generation、Index SHA及"
            "人工CNF SHA均已校验；数据截止日"
            f" {dataset.date_range[1]}。"
        )
    actual = [record for record in prepared.records if record is not None]
    if not actual:
        st.info("当前日期和产地没有业务记录，主表保留12个船期空行。")
    elif all(
        record.duty_paid_cost_cny_per_tonne is None
        and record.net_crush_margin_cny_per_tonne is None
        for record in actual
    ):
        st.warning(
            "当前日期的CNF或必要行情不完整，"
            "完税成本和盘面净榨利暂无正式结果。"
        )


def _render_daily_table(
    prepared: PreparedPageData,
    *,
    config: SoybeanImportProfitConfig,
    origin: str,
    origin_label: str,
    business_date: date,
    parameters_available: bool = True,
) -> None:
    render_section_heading(
        origin_profit_title(origin, origin_label),
        "当日船期、价格与榨利",
    )
    table = formal_daily_table(
        prepared.records,
        business_date=business_date,
        origin=origin,
        config=config,
        parameters_available=parameters_available,
    )
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        height=460,
    )
    st.caption(price_basis_note(prepared.records))


def _render_contract_override_notice(records) -> None:
    notices = []
    for record in records:
        if record is None:
            continue
        for label, mode, automatic, effective, reason in (
            (
                "CBOT Soybeans",
                record.cbot_selection_mode,
                record.cbot_automatic_contract,
                record.cbot_contract,
                record.cbot_override_reason,
            ),
            (
                "DCE Soymeal",
                record.soymeal_selection_mode,
                record.soymeal_automatic_contract,
                record.soymeal_contract,
                record.soymeal_override_reason,
            ),
            (
                "DCE Soybean Oil",
                record.soyoil_selection_mode,
                record.soyoil_automatic_contract,
                record.soyoil_contract,
                record.soyoil_override_reason,
            ),
        ):
            if mode == "manual_override":
                notices.append(
                    f"{record.shipment_period} {label}: "
                    f"{automatic} → {effective}（{reason}）"
                )
    if notices:
        st.info(
            "MANUAL CONTRACT OVERRIDE｜人工合约覆盖\n\n"
            + "\n\n".join(notices)
        )


def _render_cnf_editor(
    prepared: PreparedPageData,
    *,
    config: SoybeanImportProfitConfig,
    business_date: date,
    runtime_identity: PageRuntimeIdentity | None = None,
    allow_cnf_save: bool = False,
    runtime_save_handler: RuntimeSaveHandler | None = None,
    runtime_context_error: str | None = None,
) -> None:
    render_section_heading(
        "CNF内存预览",
        "只允许编辑CNF升贴水；正式历史值与未保存预览值分列展示",
    )
    st.warning(PREVIEW_NOTICE)
    previews = _session_previews()
    editor_frame = editable_daily_table(
        prepared.records,
        business_date=business_date,
        previews=previews,
    )
    edited_cnf = dict(st.session_state.get(STATE_EDITED_CNF, {}))
    for index, shipment_period in enumerate(editor_frame["船期"]):
        if shipment_period in edited_cnf:
            editor_frame.at[index, "CNF升贴水"] = edited_cnf[
                shipment_period
            ]
    editable_column = "CNF升贴水"
    edited = st.data_editor(
        editor_frame,
        hide_index=True,
        width="stretch",
        height=460,
        disabled=[
            column
            for column in editor_frame.columns
            if column != editable_column
        ],
        column_config={
            editable_column: st.column_config.NumberColumn(
                "CNF升贴水",
                help="允许有限正数、有限负数、0或清空。",
                format="%.2f",
            )
        },
        key=(
            "import_profit_page:cnf_editor:"
            f"{st.session_state.get(STATE_EDITOR_VERSION, 0)}"
        ),
    )
    action_columns = st.columns((1, 1, 4))
    recalculate = action_columns[0].button(
        "重新计算预览",
        type="primary",
        key="import_profit_page:recalculate_preview",
    )
    restore = action_columns[1].button(
        "恢复原始CNF",
        key="import_profit_page:restore_original",
    )
    if restore:
        restore_original_preview_state(st.session_state)
        st.rerun()
    if recalculate:
        try:
            calculated = previews_from_editor(
                prepared.records, edited, config
            )
        except InvalidCnfPreviewError as exc:
            st.error(str(exc))
        except ImportProfitPageError:
            st.error("预览输入与正式查询记录不兼容，未更新预览。")
        except Exception:
            st.error("预览计算失败，最后一次合法预览保持不变。")
        else:
            cnf_values = {
                str(row["船期"]): (
                    None
                    if pd.isna(row["CNF升贴水"])
                    else float(row["CNF升贴水"])
                )
                for _, row in edited.iterrows()
            }
            st.session_state[STATE_EDITED_CNF] = cnf_values
            st.session_state[STATE_LAST_LEGAL_EDIT] = dict(cnf_values)
            st.session_state[STATE_PREVIEWS] = calculated
            st.rerun()
    if previews:
        success = sum(
            preview.preview_status == "success"
            for preview in previews.values()
        )
        st.success(
            f"未保存预览值已更新：成功 {success} 个，"
            f"仍不完整 {len(previews) - success} 个。"
        )
    if runtime_identity is None:
        return
    context = st.session_state.get(STATE_RUNTIME_CONTEXT)
    if runtime_context_error:
        st.error(runtime_context_error)
        return
    if not isinstance(context, RuntimeEditorContext):
        st.error("正式编辑上下文无效，请重新加载最新数据。")
        return
    stale = runtime_context_is_stale(context, runtime_identity)
    if stale:
        st.warning(
            "运行数据已更新，当前编辑基于旧版本，请重新加载最新数据。"
        )
        if st.button(
            "重新加载最新数据",
            key="import_profit_page:reload_runtime",
        ):
            reset_runtime_editor_state(st.session_state)
            st.rerun()
    try:
        differences = cnf_differences_from_editor(context, edited)
        input_error = None
    except InvalidCnfPreviewError as exc:
        differences = ()
        input_error = str(exc)
        st.error(input_error)
    render_section_heading(
        "正式CNF保存",
        "仅提交实际变化船期；一次点击对应一次批量Release事务",
    )
    if differences:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "船期": item.shipment_period,
                        "原CNF": item.previous_cnf,
                        "新CNF": item.new_cnf,
                        "原来源": item.previous_source or "—",
                        "变化类型": item.change_type,
                    }
                    for item in differences
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("当前没有需要写入的业务变化。")
    confirmed = st.checkbox(
        "我确认将上述CNF修改写入正式运行数据，并更新对应完税成本和盘面净榨利。",
        key="import_profit_page:confirm_runtime_save",
    )
    save_disabled = (
        not allow_cnf_save
        or runtime_save_handler is None
        or not differences
        or input_error is not None
        or stale
        or not confirmed
    )
    save_clicked = st.button(
        "保存CNF并更新正式结果",
        type="primary",
        disabled=save_disabled,
        key="import_profit_page:save_runtime_cnf",
    )
    if not allow_cnf_save:
        st.info("当前运行入口未开放CNF正式保存。")
    if not save_clicked or runtime_save_handler is None:
        return
    with st.spinner("正在保存CNF并构建新的正式Release，请稍等..."):
        feedback = runtime_save_handler(context, differences)
    if feedback.status == "success":
        reset_runtime_editor_state(st.session_state)
        st.session_state[STATE_SAVE_MESSAGE] = feedback.message
        st.rerun()
    elif feedback.status == "no_change":
        st.info("没有需要写入的业务变化。")
    elif feedback.status == "concurrent_conflict":
        st.warning(
            "运行数据已被其他会话更新，本次保存未执行，"
            "请重新加载最新数据。"
        )
        if st.button(
            "重新加载最新数据",
            key="import_profit_page:reload_after_conflict",
        ):
            reset_runtime_editor_state(st.session_state)
            st.rerun()
    elif feedback.status == "locked":
        st.warning("运行数据正在执行其他更新，请稍后重试。")
    elif feedback.status == "validation_failed":
        st.error(feedback.message)
    else:
        st.error("正式更新失败，当前运行版本保持不变。")


def _render_recent_matrices(
    prepared: PreparedPageData,
    *,
    origin_label: str,
    business_date: date,
) -> None:
    render_section_heading(
        "最近10个工作日",
        f"{origin_label}｜截止{business_date}｜正式历史值",
    )
    tabs = st.tabs([item[1] for item in METRIC_SECTIONS])
    for tab, (metric, _, title, _) in zip(
        tabs, METRIC_SECTIONS, strict=True
    ):
        with tab:
            matrix = prepared.matrix(metric)
            frame = matrix_display_frame(matrix)
            if all(
                cell.value is None
                for row in matrix.rows
                for cell in row.cells
            ):
                st.info(f"{title}在所选窗口内全部为空。")
            st.dataframe(
                frame,
                hide_index=True,
                width="stretch",
                height=420,
            )


def _render_seasonality(
    prepared: PreparedPageData,
    *,
    business_date: date,
) -> None:
    render_section_heading(
        "季节性图",
        "当前船期年份、此前5年及前五年均值；不补值、不平滑",
    )
    labels = ["CNF季节性", "到港完税成本季节性", "盘面净榨利季节性"]
    tabs = st.tabs(labels)
    for tab, (metric, _, _, unit) in zip(
        tabs, METRIC_SECTIONS, strict=True
    ):
        with tab:
            seasonal_items = prepared.seasonality(metric)
            for start in range(0, 12, 2):
                columns = st.columns(2)
                for column, seasonal in zip(
                    columns,
                    seasonal_items[start : start + 2],
                    strict=True,
                ):
                    with column:
                        message = current_window_message(
                            seasonal, business_date
                        )
                        if message:
                            st.info(message)
                        if not seasonality_has_values(seasonal):
                            st.caption(
                                "该月份当前及历史窗口均无有效值，"
                                "图表仍保留完整断点结构。"
                            )
                        figure = seasonality_figure(
                            seasonal,
                            value_label=unit,
                        )
                        st.plotly_chart(
                            figure,
                            width="stretch",
                            key=(
                                "import_profit_page:seasonality:"
                                f"{metric.value}:{seasonal.shipment_month}"
                            ),
                        )


def _session_previews() -> Mapping[str, PreviewCalculation]:
    value = st.session_state.get(STATE_PREVIEWS, {})
    return value if isinstance(value, Mapping) else {}


def _file_signature(path: Path) -> tuple[str, int, int]:
    item = path.stat()
    if not path.is_file():
        raise FileNotFoundError(path)
    return str(path), item.st_mtime_ns, item.st_size


def _safe_missing_file_message(exc: FileNotFoundError) -> str:
    filename = Path(exc.filename).name if exc.filename else "标准候选文件"
    return f"缺少标准候选文件：{filename}"
