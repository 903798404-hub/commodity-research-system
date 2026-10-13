from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import streamlit as st

if TYPE_CHECKING:
    from streamlit.navigation.page import StreamlitPage

from navigation import research_navigation_groups
from ui_theme import (
    render_home_footer,
    render_home_header,
    render_navigation_card,
    render_section_heading,
    render_workspace_page_link,
)


def load_report_catalog(catalog_path: Path, mtime: float) -> list[dict[str, object]]:
    """Retain the catalog reader for the dedicated external-application page."""

    del mtime
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def get_external_url(card: dict[str, object]) -> str:
    """Return an explicitly configured external URL without catalog fallbacks."""

    env_name = card.get("url_env")
    if not isinstance(env_name, str) or not env_name:
        return ""
    return os.getenv(env_name, "").strip()


def get_external_app_url(catalog_path: Path, title: str) -> str:
    """Resolve a configured external app URL for the dedicated entry page."""

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


def render_home(catalog_path: Path | None = None, *, workspace_page: StreamlitPage | None = None) -> None:
    """Render the sidebar information architecture as expanded research navigation."""

    del catalog_path
    render_home_header(
        title="农产品研究工作台",
        researcher_name="徐晓冉",
        phone="13305642778",
        email="xx20236@outlook.com",
    )

    render_section_heading("研究导航")
    for group in research_navigation_groups():
        st.html(f'<h3 class="agri-navigation-group-title">{group.title}</h3>')
        for offset in range(0, len(group.items), 3):
            for column, item in zip(st.columns(3), group.items[offset:offset + 3]):
                with column:
                    if item.external_env:
                        st.html(render_navigation_card(item))
                    else:
                        with st.container(key=f"agri_home_card_{item.key}"):
                            st.html(render_navigation_card(item, include_action=False))
                            render_workspace_page_link(item, label="进入模块", workspace_page=workspace_page)

    render_home_footer("研究内容仅供参考，不构成投资建议。")
