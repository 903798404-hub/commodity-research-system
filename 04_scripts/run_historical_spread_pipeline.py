from __future__ import annotations

import subprocess
import sys
from pathlib import Path


STEPS = [
    "import_historical_prices.py",
    "build_spread_config.py",
    "calculate_historical_spreads.py",
    "plot_seasonal_spreads.py",
]


def latest_file(pattern: str, directory: Path) -> Path | None:
    files = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[0] if files else None


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    python_exe = project_root / ".venv" / "Scripts" / "python.exe"
    scripts_dir = project_root / "04_scripts"
    logs_dir = project_root / "10_logs"
    charts_dir = project_root / "06_outputs" / "charts"

    if Path(sys.executable).resolve() != python_exe.resolve():
        print(f"Pipeline must run with project venv Python: {python_exe}")
        print(f"Current Python: {sys.executable}")
        return 1

    for directory in [
        project_root / "01_data",
        project_root / "01_data" / "manual_history",
        project_root / "06_outputs",
        charts_dir,
        logs_dir,
    ]:
        directory.mkdir(parents=True, exist_ok=True)

    for step in STEPS:
        script_path = scripts_dir / step
        print(f"Running step: {step}")
        result = subprocess.run(
            [str(python_exe), str(script_path)],
            cwd=project_root,
            text=True,
            capture_output=True,
        )
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="")
        if result.returncode != 0:
            print(f"Pipeline failed at step: {step}")
            return result.returncode

    rm_png = latest_file("RM_5-9_*.png", charts_dir)
    rm_chart_data = latest_file("RM_5-9_chart_data_*.xlsx", charts_dir)
    log_files = {
        "import_historical_prices_log": latest_file("import_historical_prices_*.log", logs_dir),
        "build_spread_config_log": latest_file("build_spread_config_*.log", logs_dir),
        "calculate_historical_spreads_log": latest_file("calculate_historical_spreads_*.log", logs_dir),
        "plot_seasonal_spreads_log": latest_file("plot_seasonal_spreads_*.log", logs_dir),
    }

    print("Pipeline completed successfully.")
    print(f"historical_price_long.xlsx: {project_root / '01_data' / 'historical_price_long.xlsx'}")
    print(f"historical_spread_config.xlsx: {project_root / '02_configs' / 'historical_spread_config.xlsx'}")
    print(f"historical_spread_database.xlsx: {project_root / '01_data' / 'historical_spread_database.xlsx'}")
    print(f"RM 5-9 PNG: {rm_png}")
    print(f"RM 5-9 chart_data Excel: {rm_chart_data}")
    for label, path in log_files.items():
        print(f"{label}: {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


