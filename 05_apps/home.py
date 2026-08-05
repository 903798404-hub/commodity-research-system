from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import streamlit as st

from ui_theme import (
    render_attention_items,
    render_dashboard_card,
    render_home_footer,
    render_home_header,
    render_section_heading,
    render_status_overview,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "01_data"
PAGE_TARGETS = {
    "spreads_dashboard": "价差动态看板",
    "basis_domestic": "基差/一口价",
    "soybean_crop_progress": "美豆种植生长",
    "crop_weather": "大豆天气",
    "status": "运行监控",
}

SPREAD_STATUS_FILE = DATA_DIR / "update_status.json"
BASIS_DATABASE_FILE = DATA_DIR / "database" / "basis" / "basis_quotes.parquet"
SOYBEAN_STATUS_FILE = DATA_DIR / "update_status" / "soybean_crop_progress.json"
SOYBEAN_PROGRESS_FILE = (
    DATA_DIR / "processed" / "soybean_crop_progress" / "soybeans_crop_progress_weekly_2021_2026.parquet"
)
FOREIGN_SEATS_STATUS_FILE = DATA_DIR / "foreign_seats_update_status.json"
WEATHER_DATA_DIR_ENV = "WEATHER_DATA_DIR"


@dataclass(frozen=True)
class HomeModule:
    """Presentation-only metadata for one fixed homepage entry."""

    module_id: str
    title: str
    marker: str
    description: str
    source_label: str
    update_mode: str
    destination_page: str | None = None
    external_env: str | None = None
    coverage_hint: str | None = None
    overview_update_mode: str | None = None


@dataclass(frozen=True)
class ModuleStatus:
    """A small, display-safe status summary derived from existing runtime files."""

    state: str
    label: str
    detail: str
    latest_value: str
    attention: str | None = None


HOME_MODULES = (
    HomeModule(
        module_id="spreads_dashboard",
        title="市场价格",
        marker="市",
        description="查看国内期货跨期与跨品种价差结构",
        source_label="AKShare",
        update_mode="工作日 16:30",
        destination_page=PAGE_TARGETS["spreads_dashboard"],
    ),
    HomeModule(
        module_id="basis_domestic",
        title="国内现货",
        marker="现",
        description="跟踪豆油、菜油、棕榈油及两粕基差与一口价",
        source_label="历史库 + basis_price SQL",
        update_mode="SQL 更新",
        destination_page=PAGE_TARGETS["basis_domestic"],
    ),
    HomeModule(
        module_id="soybean_crop_progress",
        title="美豆周度跟踪",
        marker="周",
        description="跟踪美豆种植、生长及后续出口进度",
        source_label="USDA NASS",
        update_mode="每周更新",
        destination_page=PAGE_TARGETS["soybean_crop_progress"],
    ),
    HomeModule(
        module_id="crop_weather",
        title="作物天气研究",
        marker="天",
        description="对比大豆、菜籽、棕榈油及印度作物当前天气与历史同期异常。",
        source_label="全球主产区",
        update_mode="全球主产区",
        destination_page=PAGE_TARGETS["crop_weather"],
        coverage_hint="大豆 · 菜籽 · 棕榈油 · 印度作物",
        overview_update_mode="天气数据更新",
    ),
    HomeModule(
        module_id="usda_dashboard",
        title="USDA 供需平衡",
        marker="US",
        description="查看全球油籽油脂供需、库存及月度修正",
        source_label="独立应用",
        update_mode="外部应用",
        external_env="USDA_DASHBOARD_URL",
    ),
    HomeModule(
        module_id="oil_world_dashboard",
        title="Oil World 供需平衡",
        marker="OW",
        description="查看 Oil World 季度平衡表与供需变化",
        source_label="独立应用",
        update_mode="外部应用",
        external_env="OIL_WORLD_DASHBOARD_URL",
    ),
)


def load_report_catalog(catalog_path: Path, mtime: float) -> list[dict[str, object]]:
    """Retain the catalog reader for the dedicated external-application page."""
    del mtime
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def open_catalog_page(page_key: str) -> None:
    """Use the existing Streamlit session-state routing mechanism."""
    target = PAGE_TARGETS.get(page_key)
    if target:
        st.session_state.selected_workspace_page = target


def apply_home_navigation_request() -> None:
    """Translate a homepage card link to the app's existing sidebar route."""
    page_key = st.query_params.get("home_target")
    if not isinstance(page_key, str) or page_key not in PAGE_TARGETS:
        return
    open_catalog_page(page_key)
    del st.query_params["home_target"]


def get_external_url(card: dict[str, object]) -> str:
    """Return an explicitly configured external URL without using catalog fallbacks.

    The catalog contains local development examples. The homepage must not turn those
    examples into an implicit production address when an environment variable is absent.
    """
    env_name = card.get("url_env")
    if not isinstance(env_name, str) or not env_name:
        return ""
    return os.getenv(env_name, "").strip()


def get_external_app_url(catalog_path: Path, title: str) -> str:
    """Resolve a configured external app URL for the existing dedicated entry page."""
    if not catalog_path.exists():
        return ""
    try:
        cards = load_report_catalog(catalog_path, catalog_path.stat().st_mtime)
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    normalized_title = "".join(title.split())
    card = next(
        (
            item
            for item in cards
            if "".join(str(item.get("title", "")).split()) == normalized_title
            and item.get("type") == "external_app"
        ),
        None,
    )
    return get_external_url(card) if isinstance(card, dict) else ""


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_parquet_date(path: Path, column: str) -> str:
    if not path.exists():
        return ""
    try:
        data = pd.read_parquet(path, columns=[column])
    except (OSError, ValueError, KeyError):
        return ""
    dates = pd.to_datetime(data[column], errors="coerce").dropna()
    return dates.max().strftime("%Y-%m-%d") if not dates.empty else ""


def _weather_runtime_files() -> tuple[Path, ...]:
    """Return only the 12 dynamic weather files from the active runtime root."""

    runtime_root = Path(os.getenv(WEATHER_DATA_DIR_ENV, "").strip() or DATA_DIR / "processed" / "weather")
    if not runtime_root.is_dir():
        return ()
    return tuple(
        path
        for path in sorted(runtime_root.rglob("*_weather_*.parquet"))
        if not path.name.endswith("_30y_normal.parquet")
    )


def _weather_runtime_identity() -> str:
    """Include the mounted runtime path and every dynamic-file identity in the cache key."""

    entries: list[str] = []
    for path in _weather_runtime_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(entries)


def _weather_observed_latest() -> str:
    """Use the minimum per-file observed date as the global weather-card contract."""

    latest_dates: list[pd.Timestamp] = []
    for path in _weather_runtime_files():
        try:
            data = pd.read_parquet(path, columns=["date", "data_type"])
        except (OSError, ValueError, KeyError):
            return ""
        observed = pd.to_datetime(data.loc[data["data_type"] == "observed", "date"], errors="coerce").dropna()
        if observed.empty:
            return ""
        latest_dates.append(pd.Timestamp(observed.max()).normalize())
    return min(latest_dates).date().isoformat() if latest_dates else ""


@st.cache_data(show_spinner=False)
def load_home_statuses(
    spread_mtime: float,
    basis_mtime: float,
    soybean_mtime: float,
    weather_runtime_identity: str,
    foreign_seats_mtime: float,
    usda_url: str,
    oil_world_url: str,
) -> dict[str, ModuleStatus]:
    """Read existing, small status artifacts for homepage summaries only."""
    del spread_mtime, basis_mtime, soybean_mtime, weather_runtime_identity, foreign_seats_mtime

    spread = _read_json(SPREAD_STATUS_FILE)
    spread_latest = str(spread.get("latest_date") or "")
    spread_success = spread.get("status") == "success" and bool(spread_latest)

    basis_latest = _latest_parquet_date(BASIS_DATABASE_FILE, "date")

    soybean = _read_json(SOYBEAN_STATUS_FILE)
    soybean_latest = str(soybean.get("new_latest_week") or soybean.get("old_latest_week") or "")
    soybean_latest = soybean_latest[:10]
    if not soybean_latest:
        soybean_latest = _latest_parquet_date(SOYBEAN_PROGRESS_FILE, "week_ending")
    soybean_available = bool(soybean_latest) and soybean.get("status") not in {"failed", "error"}

    weather_observed = _weather_observed_latest()
    weather_available = bool(weather_observed)
    weather_update_text = (
        f"天气数据更新至 {weather_observed}"
        if weather_available
        else "天气数据更新时间不可用"
    )

    foreign = _read_json(FOREIGN_SEATS_STATUS_FILE).get("foreign_seats", {})
    foreign = foreign if isinstance(foreign, dict) else {}
    foreign_latest = str(foreign.get("latest_date") or "")
    foreign_partial = foreign.get("status") == "partial_failure"

    statuses = {
        "spreads_dashboard": ModuleStatus(
            state="success" if spread_success else "unavailable",
            label="自动更新" if spread_success else "暂不可用",
            detail="工作日 16:30 更新",
            latest_value=f"最新业务日 {spread_latest}" if spread_latest else "等待有效状态文件",
        ),
        "basis_domestic": ModuleStatus(
            state="success" if basis_latest else "unavailable",
            label="数据可用" if basis_latest else "暂不可用",
            detail="SQL 更新，页面读取正式 Parquet",
            latest_value=f"最新业务日 {basis_latest}" if basis_latest else "未读取到稳定数据",
            attention=(
                None
                if basis_latest
                else "国内现货稳定数据未读取到，需检查正式 Parquet。"
            ),
        ),
        "soybean_crop_progress": ModuleStatus(
            state="info" if soybean_available else "unavailable",
            label="每周更新" if soybean_available else "暂不可用",
            detail="每周二至周四 06:30 检查",
            latest_value=f"截至 {soybean_latest}" if soybean_latest else "等待有效周度状态文件",
        ),
        "crop_weather": ModuleStatus(
            state="info" if weather_available else "unavailable",
            label="",
            detail="",
            latest_value=weather_update_text,
            attention=weather_update_text,
        ),
        "usda_dashboard": ModuleStatus(
            state="external" if usda_url else "unavailable",
            label="外部应用" if usda_url else "暂不可用",
            detail="由独立应用提供服务",
            latest_value="地址已配置" if usda_url else "未配置访问地址",
            attention=(
                None
                if usda_url
                else "USDA 供需平衡入口未配置访问地址。"
            ),
        ),
        "oil_world_dashboard": ModuleStatus(
            state="external" if oil_world_url else "unavailable",
            label="外部应用" if oil_world_url else "暂不可用",
            detail="由独立应用提供服务",
            latest_value="地址已配置" if oil_world_url else "未配置访问地址",
            attention=(
                None
                if oil_world_url
                else "Oil World 供需平衡入口未配置访问地址。"
            ),
        ),
        "status": ModuleStatus(
            state="success" if spread_success else "unavailable",
            label="状态可查" if spread_success else "暂不可用",
            detail="完整日志请进入运行监控",
            latest_value=f"价差最近更新 {spread_latest}" if spread_latest else "等待运行状态文件",
        ),
        "foreign_seats": ModuleStatus(
            state="warning" if foreign_partial else "success",
            label="部分可用" if foreign_partial else "状态待核验",
            detail="AKShare 来源，DCE 官方文件兜底",
            latest_value=f"最新业务日 {foreign_latest}" if foreign_latest else "未读取到状态日期",
            attention=(
                f"外资与重点席位：部分可用，当前可信最新业务日为 {foreign_latest}；部分来源更新失败，数据待核验。"
                if foreign_partial
                else None
            ),
        ),
    }
    return statuses


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def get_home_statuses() -> dict[str, ModuleStatus]:
    return load_home_statuses(
        _mtime(SPREAD_STATUS_FILE),
        _mtime(BASIS_DATABASE_FILE),
        max(_mtime(SOYBEAN_STATUS_FILE), _mtime(SOYBEAN_PROGRESS_FILE)),
        _weather_runtime_identity(),
        _mtime(FOREIGN_SEATS_STATUS_FILE),
        os.getenv("USDA_DASHBOARD_URL", "").strip(),
        os.getenv("OIL_WORLD_DASHBOARD_URL", "").strip(),
    )


def home_modules() -> tuple[HomeModule, ...]:
    """Expose the fixed entry contract for tests and future presentation work."""
    return HOME_MODULES


def _overall_status(statuses: dict[str, ModuleStatus]) -> tuple[str, str]:
    core_ids = ("spreads_dashboard", "basis_domestic", "soybean_crop_progress", "crop_weather")
    all_available = all(statuses[module_id].state != "unavailable" for module_id in core_ids)
    return (
        ("success", "核心模块状态正常")
        if all_available
        else ("warning", "部分模块需要关注")
    )


def render_home(catalog_path: Path | None = None) -> None:
    """Render the fixed research-workbench homepage without changing routes."""
    del catalog_path  # External addresses are deliberately environment-only on this page.
    statuses = get_home_statuses()
    overall_state, overall_label = _overall_status(statuses)
    render_home_header(
        title="农产品研究工作台",
        description="汇集市场价格、国内现货、周度跟踪、作物天气与国际供需数据",
        date_text=dt.date.today().strftime("%Y年%m月%d日"),
        state=overall_state,
        state_label=overall_label,
        update_hint="各模块更新时间见下方状态",
    )

    render_section_heading("核心研究入口", "六个稳定入口，研究模块内部承载后续数据扩展。")
    cards_markup = "".join(
        render_dashboard_card(module, statuses[module.module_id]) for module in HOME_MODULES
    )
    # ``st.html`` keeps the collection as one DOM subtree; ``st.markdown``
    # treats nested HTML blocks as separate markdown sections and breaks CSS grid.
    st.html(f'<section class="agri-card-area"><div class="agri-card-collection">{cards_markup}</div></section>')

    attention_items = [
        ("国内现货", statuses["basis_domestic"].attention),
        ("作物天气", statuses["crop_weather"].attention),
        ("外资与重点席位", statuses["foreign_seats"].attention),
        ("USDA 供需平衡", statuses["usda_dashboard"].attention),
        ("Oil World 供需平衡", statuses["oil_world_dashboard"].attention),
    ]
    active_attention_items = [(title, message) for title, message in attention_items if message]
    render_section_heading("需要关注", "仅列出人工维护、部分可用或入口不可用等真实事项。")
    if active_attention_items:
        render_attention_items([(title, str(message)) for title, message in active_attention_items])
    else:
        st.caption("当前没有需要首页提示的额外事项。")

    render_section_heading("数据状态概览", "完整日志、任务细节和错误信息请进入运行监控页面。")
    st.caption("[进入运行监控](?home_target=status)")
    overview_ids = (
        "spreads_dashboard",
        "basis_domestic",
        "soybean_crop_progress",
        "crop_weather",
        "usda_dashboard",
        "oil_world_dashboard",
        "foreign_seats",
    )
    render_status_overview(
        [
            (
                module.title,
                module.source_label,
                module.overview_update_mode or module.update_mode,
                statuses[module.module_id],
            )
            for module in HOME_MODULES
            if module.module_id in overview_ids
        ]
        + [
            (
                "外资与重点席位",
                "AKShare / DCE 官方文件",
                "待核验",
                statuses["foreign_seats"],
            )
        ]
    )
    render_home_footer("数据仅供研究参考，不构成投资建议；数据来源：AKShare、USDA、Oil World 及官方渠道。")
