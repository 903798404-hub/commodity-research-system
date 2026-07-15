from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / "06_outputs" / "special_time_axis_preview"
SPECIAL = {("Soybeans", "Brazil"), ("Sunflowerseed", "Argentina")}
MODEL_FIELDS = {"period_family", "period_basis", "source_role"}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def strip_model_fields(payload: dict) -> dict:
    cleaned = dict(payload)
    cleaned["metrics"] = [{key: value for key, value in metric.items() if key not in MODEL_FIELDS} for metric in payload["metrics"]]
    return cleaned


def metric_key(metric: dict) -> tuple[str, str, str]:
    return metric["metric"], metric["period_family"], metric["source_role"]


def special_preview(release: str, filename: str) -> dict:
    payload = read_json(PREVIEW / "releases" / release / "combinations" / filename)
    return {"periods": payload["periods"], "market_year_basis": payload["market_year_basis"], "metrics": {"|".join(metric_key(metric)): metric for metric in payload["metrics"]}}


def main() -> None:
    unchanged_differences: list[dict] = []
    changed_combinations: dict[str, list[str]] = {}
    for release in ("2026-03", "2026-06"):
        old_root = ROOT / "01_data" / "releases" / release
        new_root = PREVIEW / "releases" / release
        old_index = read_json(old_root / "index.json")
        changed: list[str] = []
        for item in old_index["files"]:
            identity = (item["product"], item["region"])
            old = read_json(old_root / item["path"])
            new = read_json(new_root / item["path"])
            if strip_model_fields(old) != strip_model_fields(new):
                changed.append(f"{item['product']} / {item['region']}")
                if identity not in SPECIAL:
                    unchanged_differences.append({"release": release, "product": item["product"], "region": item["region"]})
        changed_combinations[release] = changed

    comparison_old = read_json(ROOT / "01_data" / "comparisons" / "2026-03_to_2026-06" / "index.json")
    comparison_new = read_json(PREVIEW / "comparisons" / "2026-03_to_2026-06" / "index.json")
    report = {
        "passed": not unchanged_differences and all(len(items) == 2 for items in changed_combinations.values()),
        "formal_unchanged": {
            "release_2026_03_tree": tree_hash(ROOT / "01_data" / "releases" / "2026-03"),
            "release_2026_06_tree": tree_hash(ROOT / "01_data" / "releases" / "2026-06"),
            "comparison_tree": tree_hash(ROOT / "01_data" / "comparisons" / "2026-03_to_2026-06"),
            "latest_sha256": sha256(ROOT / "public" / "data" / "oil_world" / "latest.json"),
            "releases_sha256": sha256(ROOT / "public" / "data" / "oil_world" / "releases.json"),
        },
        "preview_tree_hashes": {
            "release_2026_03": tree_hash(PREVIEW / "releases" / "2026-03"),
            "release_2026_06": tree_hash(PREVIEW / "releases" / "2026-06"),
            "comparison": tree_hash(PREVIEW / "comparisons" / "2026-03_to_2026-06"),
        },
        "changed_combinations": changed_combinations,
        "other_57_business_differences": unchanged_differences,
        "comparison_counts": {"old": comparison_old, "new": comparison_new},
        "special": {
            "2026-03_brazil_soybeans": special_preview("2026-03", "soybeans__brazil.json"),
            "2026-06_brazil_soybeans": special_preview("2026-06", "soybeans__brazil.json"),
            "2026-03_argentina_sunflowerseed": special_preview("2026-03", "sunflowerseed__argentina.json"),
            "2026-06_argentina_sunflowerseed": special_preview("2026-06", "sunflowerseed__argentina.json"),
        },
    }
    destination = PREVIEW / "special_time_axis_audit.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "audit": str(destination), "changed": changed_combinations, "other_57_differences": len(unchanged_differences)}, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
