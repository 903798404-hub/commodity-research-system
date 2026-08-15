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
OPTIONAL_FIELDS = {"contract", "frontend_tests", "contract_tests"}
PATH_FIELDS = {
    "code_paths",
    "direct_tests",
    "impact_tests",
    "frontend_tests",
    "contract_tests",
}
FOUNDATION_REQUIRED_MODULES = {
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
    assert isinstance(payload["modules"], dict)
    assert FOUNDATION_REQUIRED_MODULES <= set(payload["modules"])
    for module_id, module in payload["modules"].items():
        assert isinstance(module_id, str)
        assert module_id == module_id.strip()
        assert "." in module_id
        assert isinstance(module, dict)
        assert REQUIRED_FIELDS <= set(module)
        assert set(module) <= REQUIRED_FIELDS | OPTIONAL_FIELDS

        for field in PATH_FIELDS & set(module):
            values = module[field]
            assert isinstance(values, list)
            assert all(isinstance(value, str) and value for value in values)
            assert len(values) == len(set(values))
        assert module["code_paths"]
        assert module["direct_tests"]

        dependents = module["dependents"]
        assert isinstance(dependents, list)
        assert all(isinstance(value, str) and value for value in dependents)
        assert len(dependents) == len(set(dependents))
        assert isinstance(module["full_regression_when_changed"], bool)
        if "contract" in module:
            assert isinstance(module["contract"], bool)


def test_all_declared_code_and_test_paths_resolve_inside_repository() -> None:
    modules = yaml.safe_load(MAP_PATH.read_text(encoding="utf-8"))["modules"]
    root = PROJECT_ROOT.resolve()
    for declaration in modules.values():
        for field in PATH_FIELDS & set(declaration):
            for value in declaration[field]:
                assert "\\" not in value
                assert not Path(value).is_absolute()
                assert ".." not in Path(value).parts
                resolved = (root / value).resolve(strict=True)
                assert root in resolved.parents


def test_domains_spreads_registration() -> None:
    modules = yaml.safe_load(MAP_PATH.read_text(encoding="utf-8"))["modules"]
    assert "domains.spreads" in modules
    spreads = modules["domains.spreads"]
    domain_prefix = "03_src/agri_research_agent/domains/spreads/"
    test_prefix = "08_tests/domains/spreads/"
    assert any(path.startswith(domain_prefix) for path in spreads["code_paths"])
    assert spreads["direct_tests"]
    assert all(path.startswith(test_prefix) for path in spreads["direct_tests"])
    assert "08_tests/test_streamlit_dashboard.py" in spreads["impact_tests"]


def test_runtime_context_requires_full_regression() -> None:
    modules = yaml.safe_load(MAP_PATH.read_text(encoding="utf-8"))["modules"]
    assert modules["shared.runtime_context"]["full_regression_when_changed"] is True
