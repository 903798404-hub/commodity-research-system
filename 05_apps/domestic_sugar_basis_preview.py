"""Local preview of the independent sugar basis page."""
from basis_page import PROJECT_ROOT  # Initializes the shared source path.
from domestic_sugar_basis import render_sugar_basis_page
import streamlit as st

st.set_page_config(page_title="白糖基差", layout="wide")
st.caption("本机预览：只读已核验快照。")
render_sugar_basis_page()
