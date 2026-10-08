"""Commodity funds and fixed-seat positioning in the existing workspace route."""
from pathlib import Path

import streamlit as st

from agri_research_agent.positions.workspace import DOMAINS, data_root
from oilseed_positions_page import render_oilseed_positions_page
from sugar_positions_page import render_sugar_positions_page


def render_foreign_seats_page(project_root: Path) -> None:
    domain = st.radio("持仓板块", list(DOMAINS), format_func=DOMAINS.get,
                      horizontal=True, key="commodity_positions_domain")
    root = data_root(project_root, domain)
    if domain == "sugar":
        render_sugar_positions_page(project_root, data_root=root, preview_mode=False)
    else:
        render_oilseed_positions_page(project_root, domain, data_root=root, preview_mode=False)
