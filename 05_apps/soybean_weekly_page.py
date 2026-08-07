"""One weekly soybean workspace route with two first-level research tabs."""

from __future__ import annotations

import streamlit as st

from soybean_crop_progress_page import render_soybean_crop_progress_page
from soybean_exports_page import render_soybean_exports_page


def render_soybean_weekly_page() -> None:
    planting_tab, exports_tab = st.tabs(["种植生长", "出口销售与装船"])
    with planting_tab:
        render_soybean_crop_progress_page()
    with exports_tab:
        render_soybean_exports_page()
