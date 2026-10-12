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


def test_explicit_dce_source_updates_all_three_summaries_and_keeps_old_and_foreign_rows(tmp_path,monkeypatch):
    from test_server_collection import Source
    baseline=archive(day='2026-10-08')
    monkeypatch.setattr(collection.delivery,'_module',lambda *a:type('Collector',(),{'collect':staticmethod(lambda *a,**k:([],[],[],[]))}))
    root,attempts=collection.collect_bundle(baseline,tmp_path,markets='domestic',today=date(2026,10,9),stock_source=Source())
    candidate=delivery.archive_bundle(root,collection.delivery.ROOT)
    before=delivery.validate_archive(baseline,collection.delivery.ROOT)[0]
    after=delivery.validate_archive(candidate,collection.delivery.ROOT)[0]
    for domain in delivery.DOMAINS:
        assert after[domain]['foreign']==before[domain]['foreign']
        assert all(row in after[domain]['domestic'] for row in before[domain]['domestic'])
    assert {'M','Y'}<={r['scope'] for r in after['soybean']['domestic']}
    assert 'P' in {r['scope'] for r in after['palm']['domestic']}
    assert len([a for a in attempts if a['source_id'].startswith('stockapi_') and a['status']=='ok'])==2
    assert not delivery.observations(candidate,baseline,collection.delivery.ROOT)['revised_partitions']


def test_dce_failure_keeps_published_values_and_reports_both_missing_boards_without_secret(tmp_path,monkeypatch):
    from agri_research_agent.oilseed_positions.stock_api import StockApiError
    class Broken:
        def holdings(self,*a,**k):raise StockApiError('upstream echoed a private-test-token')
    baseline=archive(day='2026-10-08')
    monkeypatch.setattr(collection.delivery,'_module',lambda *a:type('Collector',(),{'collect':staticmethod(lambda *a,**k:([],[],[],[]))}))
    root,attempts=collection.collect_bundle(baseline,tmp_path,markets='domestic',today=date(2026,10,9),stock_source=Broken())
    candidate=delivery.archive_bundle(root,collection.delivery.ROOT)
    assert not delivery.observations(candidate,baseline,collection.delivery.ROOT)['business_changed']
    assert {a['domain'] for a in attempts if a['status']=='failed'}=={'soybean','palm'}
    assert 'private-test-token' not in str(attempts)


def test_dce_source_cannot_be_added_to_foreign_collection(tmp_path):
    with pytest.raises(ValueError,match='domestic only'):
        collection.collect_bundle(archive(),tmp_path,markets='foreign',today=date(2026,10,9),stock_source=object())
