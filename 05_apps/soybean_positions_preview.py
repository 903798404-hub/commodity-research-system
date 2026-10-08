from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "03_src"))
sys.path.insert(0, str(ROOT / "05_apps"))
import streamlit as st
from oilseed_positions_page import render_oilseed_positions_page

st.set_page_config(page_title="大豆资金情绪", page_icon="📊", layout="wide")
render_oilseed_positions_page(ROOT, "soybean")
