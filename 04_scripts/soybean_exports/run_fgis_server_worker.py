"""Pinned isolated worker; only /output is writable."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'03_src'))
from agri_research_agent.soybean_exports.fgis_server import main
if __name__=='__main__':raise SystemExit(main())
