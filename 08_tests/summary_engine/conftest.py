from __future__ import annotations

import sys
from pathlib import Path


APPS_DIR = Path(__file__).resolve().parents[2] / "05_apps"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))
