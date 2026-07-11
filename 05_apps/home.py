from __future__ import annotations

import json
from pathlib import Path

import streamlit as st


CATEGORY_ORDER = ["价格", "价差", "席位", "运行监控"]
PAGE_TARGETS = {
    "basis_domestic": "基差/一口价",
    "spreads_dashboard": "价差动态看板",
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


def render_card(card: dict[str, object], index: int) -> None:
    enabled = bool(card.get("enabled"))
    with st.container(border=True):
        title_col, status_col = st.columns([4, 1])
        with title_col:
            st.markdown(f"#### {card.get('title', '')}")
        with status_col:
            st.caption("可用" if enabled else "待接入")
        st.caption(f"品种：{card.get('commodities', '-')}　地区：{card.get('region', '-')}")
        st.write(card.get("description", ""))
        if enabled:
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
        columns = st.columns(min(3, len(category_cards)))
        for index, card in enumerate(category_cards):
            with columns[index % len(columns)]:
                render_card(card, index)
