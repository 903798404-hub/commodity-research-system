from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from oil_world_data.workbook_rebuild_audit import audit_rebuild, write_audit_reports  # noqa: E402


def main() -> int:
    result = audit_rebuild(PROJECT_ROOT)
    write_audit_reports(PROJECT_ROOT, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
