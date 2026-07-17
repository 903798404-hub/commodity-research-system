from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st


CATEGORY_ORDER = ["价格", "价差", "供需", "北美种植与销售", "席位", "运行监控"]
PAGE_TARGETS = {
    "basis_domestic": "基差/一口价",
    "spreads_dashboard": "价差动态看板",
    "soybean_crop_progress": "美豆种植生长",
    "status": "运行监控",
}


@st.cache_data(show_spinner=False)
def load_report_catalog(catalog_path: Path, mtime: float) -> list[dict[str, object]]:
    del mtime
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def open_catalog_page(page_key: str) -> None:
    target = PAGE_TARGETS.get(page_key)
    if target:
        st.session_state.selected_workspace_page = target


def catalog_column_count(card_count: int) -> int:
    """Use at most three catalog columns while preserving the existing wrap behavior."""
    return min(3, card_count)


def get_external_url(card: dict[str, object]) -> str:
    """Return an external application URL, preferring its configured environment override."""
    env_name = card.get("url_env")
    if isinstance(env_name, str) and env_name:
        env_url = os.getenv(env_name, "").strip()
        if env_url:
            return env_url

    configured_url = card.get("url")
    return configured_url.strip() if isinstance(configured_url, str) else ""


def get_external_app_url(catalog_path: Path, title: str) -> str:
    """Resolve an external app URL from the shared report catalog configuration."""
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


def render_card(card: dict[str, object], index: int) -> None:
    enabled = bool(card.get("enabled"))
    is_external_app = card.get("type") == "external_app"
    with st.container(border=True):
        title_col, status_col = st.columns([4, 1])
        with title_col:
            st.markdown(f"#### {card.get('title', '')}")
        with status_col:
            st.caption("可用" if enabled else "待接入")
        st.caption(f"品种：{card.get('commodities', '-')}　地区：{card.get('region', '-')}")
        st.write(card.get("description", ""))
        if enabled and is_external_app:
            external_url = get_external_url(card)
            if external_url:
                st.link_button(
                    "打开",
                    external_url,
                    use_container_width=True,
                )
            else:
                st.button(
                    "未配置地址",
                    key=f"catalog_external_missing_{index}",
                    disabled=True,
                    use_container_width=True,
                    help="请在报告目录配置或对应环境变量中设置访问地址。",
                )
        elif enabled:
            st.button(
                "打开",
                key=f"catalog_open_{card.get('page_key')}_{index}",
                use_container_width=True,
                on_click=open_catalog_page,
                args=(str(card.get("page_key", "")),),
            )
        else:
            st.button(
                "待接入",
                key=f"catalog_disabled_{card.get('page_key')}_{index}",
                disabled=True,
                use_container_width=True,
            )


def render_home(catalog_path: Path) -> None:
    st.title("油脂油料研究工作台")
    st.caption("研究主题目录")

    if not catalog_path.exists():
        st.error(f"未找到报告目录配置：{catalog_path}")
        return

    try:
        cards = load_report_catalog(catalog_path, catalog_path.stat().st_mtime)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        st.error(f"报告目录配置读取失败：{exc}")
        return

    for category in CATEGORY_ORDER:
        category_cards = [card for card in cards if card.get("category") == category]
        if not category_cards:
            continue
        st.subheader(category)
        columns = st.columns(catalog_column_count(len(category_cards)))
        for index, card in enumerate(category_cards):
            with columns[index % len(columns)]:
                render_card(card, index)
