from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = PROJECT_ROOT / "02_configs" / "module_test_map.yaml"
REQUIRED_FIELDS = {
    "code_paths",
    "direct_tests",
    "impact_tests",
    "dependents",
    "full_regression_when_changed",
}
EXPECTED_MODULES = {
    "market_data.contracts",
    "market_data.quotes",
    "market_data.calendars",
    "market_data.loader",
    "market_data.providers.akshare",
    "shared.file_identity",
    "shared.atomic_storage",
    "shared.runtime_context",
}


def test_module_test_map_schema_and_required_modules() -> None:
    payload = yaml.safe_load(MAP_PATH.read_text(encoding="utf-8"))
    assert set(payload) == {"schema_version", "modules"}
    assert payload["schema_version"] == 1
    assert set(payload["modules"]) == EXPECTED_MODULES
    for module in payload["modules"].values():
        assert REQUIRED_FIELDS <= set(module)


def test_all_declared_code_and_test_paths_resolve_inside_repository() -> None:
    modules = yaml.safe_load(MAP_PATH.read_text(encoding="utf-8"))["modules"]
    root = PROJECT_ROOT.resolve()
    for declaration in modules.values():
        for field in ("code_paths", "direct_tests", "impact_tests"):
            for value in declaration[field]:
                assert not Path(value).is_absolute()
                assert ".." not in Path(value).parts
                resolved = (root / value).resolve(strict=True)
                assert root in resolved.parents


def test_runtime_context_requires_full_regression() -> None:
    modules = yaml.safe_load(MAP_PATH.read_text(encoding="utf-8"))["modules"]
    assert modules["shared.runtime_context"]["full_regression_when_changed"] is True
