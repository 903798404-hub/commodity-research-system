"""Canadian canola research tabs with independent data consumers."""
import streamlit as st

from canada_canola_page import render_canada_canola_page
from canola_exports_page import render_canola_exports_page


def render_canada_canola_weekly_page() -> None:
    crop, exports = st.tabs(["种植生长", "周度出口"])
    with crop:
        render_canada_canola_page()
    with exports:
        render_canola_exports_page()
