"""Official collection preserves manual sources and halts historical revisions."""
from datetime import date

import pytest

from agri_research_agent.automation import positions_scheduled_collection as collection
from agri_research_agent.positions import delivery
from test_positions_delivery import archive


@pytest.mark.parametrize("markets, expected", [("domestic", ["sugar", "rapeseed"]),
                                             ("foreign", ["sugar", "rapeseed", "soybean"])])
def test_only_selected_implemented_sources_and_one_domestic_day(tmp_path, monkeypatch, markets, expected):
    baseline = archive()
    calls = []
    def collect(source, spec=None, **kwargs):
        domain = "sugar" if spec is None else "rapeseed" if "RS" in spec["domestic"] else "soybean"
        calls.append(domain)
        assert kwargs["start_day"] == kwargs["end_day"] == date(2026, 10, 9)
        assert not kwargs.get("sina_contracts") and not kwargs.get("seed_contract")
        return [], [], [], [{"source_id": "official", "status": "not_published"}]
    monkeypatch.setattr(collection.delivery, "_module", lambda *a: type("Collector", (), {"collect": staticmethod(collect)}))
    root, attempts = collection.collect_bundle(baseline, tmp_path, markets=markets, today=date(2026, 10, 9))
    candidate = delivery.archive_bundle(root, collection.delivery.ROOT)
    assert calls == expected
    assert not delivery.observations(candidate, baseline, collection.delivery.ROOT)["business_changed"]
    assert delivery.validate_archive(candidate, collection.delivery.ROOT)[0]["palm"] == delivery.validate_archive(baseline, collection.delivery.ROOT)[0]["palm"]
    assert len(attempts) == len(expected)


def test_historical_revisions_are_not_automatically_admitted(tmp_path, monkeypatch):
    baseline = archive()
    monkeypatch.setattr(collection.delivery, "_module", lambda *a: type("Collector", (), {
        "collect": staticmethod(lambda *a, **k: ([], [], [], []))}))
    monkeypatch.setattr(delivery, "observations", lambda *a: {"revised_partitions": ["sugar/foreign/old-report"]})
    with pytest.raises(ValueError, match="historical revision requires review"):
        collection.collect_bundle(baseline, tmp_path, markets="foreign", today=date(2026, 10, 9))
