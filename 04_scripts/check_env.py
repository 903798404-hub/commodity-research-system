from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path


REQUIRED_MODULES = [
    ("pandas", "pandas"),
    ("openpyxl", "openpyxl"),
    ("akshare", "akshare"),
]


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    output_dir = project_root / "06_outputs"
    output_file = output_dir / "env_check.xlsx"

    failed_imports: list[tuple[str, Exception]] = []
    imported_modules: dict[str, object] = {}

    for package_name, import_name in REQUIRED_MODULES:
        try:
            imported_modules[package_name] = importlib.import_module(import_name)
            print(f"[OK] {package_name} imported successfully")
        except Exception as exc:  # noqa: BLE001
            failed_imports.append((package_name, exc))
            print(f"[FAILED] {package_name}: {exc}")

    if failed_imports:
        print("Environment check failed. No Excel file was generated.")
        return 1

    if output_file.exists():
        print(f"Output file already exists, will not overwrite: {output_file}")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    pandas = imported_modules["pandas"]
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    df = pandas.DataFrame(
        [
            {
                "status": "环境测试成功",
                "checked_at": now_text,
            }
        ]
    )
    df.to_excel(output_file, index=False)

    print(f"Environment check succeeded. Excel file generated: {output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

