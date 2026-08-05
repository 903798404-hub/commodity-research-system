"""更新本地国内基差数据库并启动正式 Streamlit 面板。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
IMPORT_SCRIPT = PROJECT_ROOT / "04_scripts" / "update_basis_data.py"
STREAMLIT_ENTRY = PROJECT_ROOT / "05_apps" / "streamlit_app.py"
DATABASE_FILE = (
    PROJECT_ROOT / "01_data" / "database" / "basis" / "basis_quotes.parquet"
)


def read_database_summary(database_path: Path) -> dict[str, object]:
    if not database_path.is_file():
        raise FileNotFoundError(f"正式基差数据库不存在：{database_path}")

    data = pd.read_parquet(
        database_path,
        columns=["date", "commodity", "region"],
    )
    if data.empty:
        raise ValueError(f"正式基差数据库为空：{database_path}")

    dates = pd.to_datetime(data["date"], errors="coerce")
    latest_date = dates.max()
    if pd.isna(latest_date):
        raise ValueError("正式基差数据库没有有效日期")

    return {
        "latest_date": latest_date,
        "rows": len(data),
        "commodities": data["commodity"].dropna().nunique(),
        "regions": data["region"].dropna().nunique(),
    }


def main() -> int:
    print("[INFO] 正在更新本地国内基差数据库...")
    import_result = subprocess.run(
        [sys.executable, str(IMPORT_SCRIPT)],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if import_result.returncode != 0:
        print(
            "[ERROR] 国内基差数据导入失败，面板未启动。"
            "请根据上方错误信息检查 basis_price SQL。",
            file=sys.stderr,
        )
        return import_result.returncode or 1

    try:
        summary = read_database_summary(DATABASE_FILE)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[ERROR] 正式基差数据库检查失败，面板未启动：{exc}",
            file=sys.stderr,
        )
        return 1

    print("[OK] 正式基差数据库已更新")
    print(f"最新日期：{summary['latest_date']:%Y-%m-%d}")
    print(f"总行数：{summary['rows']}")
    print(f"品种数量：{summary['commodities']}")
    print(f"地区数量：{summary['regions']}")
    print("[INFO] 正在启动本地 Streamlit 面板...")

    dashboard_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(STREAMLIT_ENTRY),
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    return dashboard_result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
