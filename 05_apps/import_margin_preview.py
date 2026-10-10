"""Standalone local entry using the same renderers as the shared workbench."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "03_src"))

from streamlit_app import main

main()
