"""Isolated read-only local preview; production entrypoint is unchanged."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "03_src"))
sys.path.insert(0, str(ROOT / "05_apps"))

import streamlit as st
from sugar_positions_page import render_sugar_positions_page

st.set_page_config(page_title="白糖资金情绪", page_icon="📊", layout="wide")
render_sugar_positions_page(ROOT)
