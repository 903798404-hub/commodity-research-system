from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_domains_spreads_has_complete_scoped_test_mapping() -> None:
    payload = yaml.safe_load(
        (PROJECT_ROOT / "02_configs" / "module_test_map.yaml").read_text(encoding="utf-8")
    )
    mapping = payload["modules"]["domains.spreads"]
    assert mapping == {
        "code_paths": [
            "03_src/agri_research_agent/domains/spreads/models.py",
            "03_src/agri_research_agent/domains/spreads/parsing.py",
            "03_src/agri_research_agent/domains/spreads/calculation.py",
            "03_src/agri_research_agent/domains/spreads/history.py",
            "05_apps/streamlit_app.py",
        ],
        "direct_tests": [
            "08_tests/domains/spreads/test_legacy_characterization.py",
            "08_tests/domains/spreads/test_models_calculation.py",
            "08_tests/domains/spreads/test_parsing.py",
            "08_tests/domains/spreads/test_history.py",
            "08_tests/domains/spreads/test_real_reference_equivalence.py",
            "08_tests/domains/spreads/test_page_contract.py",
            "08_tests/domains/spreads/test_module_map.py",
        ],
        "impact_tests": ["08_tests/test_streamlit_dashboard.py"],
        "dependents": ["apps.streamlit_dashboard"],
        "full_regression_when_changed": False,
    }
