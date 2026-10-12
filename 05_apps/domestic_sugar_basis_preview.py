"""Local preview of the real domestic basis page with an explicit sugar snapshot."""
from pathlib import Path
import os
import matplotlib
matplotlib.use("Agg")
import streamlit as st
from basis_page import render_basis_page
from agri_research_agent.market_data.activated_runtime import resolve_public_data_root

st.set_page_config(page_title="国内基差现货 · 柳州白糖", layout="wide")
ROOT = Path(__file__).resolve().parents[1]
st.caption("本机预览：独立读取已校验历史，线上页面与自动更新未变。")
runtime = Path(os.environ.get("PUBLIC_MARKET_DATA_RUNTIME_ROOT", str(ROOT.parents[1] / "market-data-worktree-runtime" / "international-spread")))
render_basis_page(resolve_public_data_root(runtime) / "public-market-data" / "lutou-domestic-basis",
                  default_tab="白糖（柳州）")
