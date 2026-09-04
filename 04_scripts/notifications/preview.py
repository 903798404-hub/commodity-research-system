"""Explicit run, formal/fixture preview entry point (offline FakeSender)."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "03_src"))

from agri_research_agent.alerts.preview import main


if __name__ == "__main__":
    raise SystemExit(main())
