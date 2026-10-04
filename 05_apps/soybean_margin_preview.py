"""Standalone local preview of the replacement page; no production mutation."""
import os
import streamlit as st
from soybean_margin_page import render_soybean_margin_page

st.set_page_config(page_title="大豆榨利 · 新版预览", layout="wide")
os.environ['SOYBEAN_MARGIN_LOCAL_PREVIEW'] = '1'
render_soybean_margin_page(os.getenv("SOYBEAN_MARGIN_HISTORY_ROOT"))
