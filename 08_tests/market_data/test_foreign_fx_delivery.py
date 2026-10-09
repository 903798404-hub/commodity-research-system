from copy import deepcopy
from datetime import date
import json

import pytest

from agri_research_agent.market_data import foreign_fx_delivery as fx
from agri_research_agent.market_data.foreign_fx import BY_CODE, FxDataError

END = date(2024, 1, 10)


@pytest.mark.parametrize("destination,body,accepted", [(fx.ECB_LATEST_URL, b"official", True),
    ("https://unexpected.example/quote", b"official", False), (fx.ECB_LATEST_URL, b"too-large", False)])
def test_formal_fetch_ignores_proxy_ca_overrides_and_bounds_response(monkeypatch, destination, body, accepted):
    import requests
    monkeypatch.setenv("HTTPS_PROXY", "http://invalid.example:1")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "untrusted.pem")
    monkeypatch.setattr(fx, "MAX_RESPONSE_BYTES", 8)
    class Response:
        status_code = 200
        url = destination
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def iter_content(self, **_): yield body
    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get(self, url, **kwargs):
            assert self.trust_env is False and kwargs["stream"] is True
            assert kwargs.get("verify", True) is True
            return Response()
    monkeypatch.setattr(requests, "Session", Session)
    if accepted:
        assert fx.fetch_delivery_official(fx.ECB_LATEST_URL) == body
    else:
        with pytest.raises(FxDataError):
            fx.fetch_delivery_official(fx.ECB_LATEST_URL)


def official_sources(*, bcb_day="2024-01-03", ecb_day="2024-01-03", anchor_day=None, revision=False, partial=False):
    bcb = json.dumps([{"data": "02/01/2024", "valor": "5.1" if revision else "5.0"},
                      {"data": date.fromisoformat(bcb_day).strftime("%d/%m/%Y"), "valor": "5.0"}]).encode()
    codes = sorted(set(BY_CODE) - {"BRL"} - ({"CAD"} if partial else set()))
    def xml(days):
        return ("<Envelope>" + "".join(f'<Cube time="{day}"><Cube currency="USD" rate="1.25"/>' +
            "".join(f'<Cube currency="{code}" rate="2.5"/>' for code in codes) + "</Cube>" for day in days) + "</Envelope>").encode()
    anchor = date.fromisoformat(anchor_day or bcb_day).strftime("%d/%m/%Y")
    html = f"<p>Cotação de fechamento do dólar no dia {anchor}, Quarta-feira:</p><table><tr><td>{anchor}</td><td>4,9990</td><td>5,0000</td></tr></table>".encode("iso-8859-1")
    def fetch(url):
        if url == fx.BCB_LATEST_URL:
            return html
        if url == fx.ECB_LATEST_URL:
            return xml([ecb_day])
        return bcb if "api.bcb.gov.br" in url else xml(["2024-01-02", ecb_day])
    return fetch


def case(tmp_path, **kwargs):
    return fx.collect(END, tmp_path / "raw", fetcher=official_sources(**kwargs))


def test_raw_replay_latest_references_and_no_change_are_business_based(tmp_path):
    snapshot, evidence = case(tmp_path)
    actual = fx.observations(snapshot, None, evidence)
    assert actual["record_count"] == 16 and actual["added"] == 16
    assert set(actual["latest_dates"]) == set(BY_CODE)
    assert not fx.observations(snapshot, snapshot, evidence)["business_changed"]
    changed, changed_evidence = case(tmp_path / "revision", revision=True)
    revision = fx.observations(changed, snapshot, changed_evidence)
    assert revision["revised"] == 1 and revision["revision_keys"] == ["BRL/2024-01-02"]
    assert revision["revisions"][0]["previous"] == 5 and revision["revisions"][0]["current"] == 5.1
    assert revision["revisions"][0]["previous_raw_sha256"] != revision["revisions"][0]["current_raw_sha256"]


def test_provider_calendars_remain_independent_and_do_not_require_previous_natural_day(tmp_path):
    snapshot, evidence = case(tmp_path, bcb_day="2024-01-03", ecb_day="2024-01-04")
    dates = fx.observations(snapshot, None, evidence)["latest_dates"]
    assert dates["BRL"] == "2024-01-03" and dates["CAD"] == "2024-01-04"


def test_history_behind_latest_publication_is_refused_before_any_stable_write(tmp_path):
    with pytest.raises(FxDataError, match="latest publication"):
        case(tmp_path, anchor_day="2024-01-04")
    assert not (tmp_path / "daily.json").exists()


@pytest.mark.parametrize("mutation", ["raw_sha", "url", "value", "drop", "method"])
def test_raw_and_derived_provenance_tampering_is_rejected(tmp_path, mutation):
    snapshot, evidence = case(tmp_path)
    if mutation == "raw_sha":
        evidence["raw"]["bcb_history"]["sha256"] = "0" * 64
    elif mutation == "url":
        evidence["raw"]["ecb_latest"]["url"] = "https://example.com/fake.xml"
    elif mutation == "value":
        snapshot["observations"][0]["local_per_usd"] = 123
    elif mutation == "drop":
        snapshot["observations"].pop()
    else:
        snapshot["sources"][0]["method"] = "different conversion"
    with pytest.raises(FxDataError):
        fx.observations(snapshot, None, evidence)


def test_cold_partial_ecb_day_is_not_accepted(tmp_path):
    with pytest.raises(FxDataError, match="coverage"):
        case(tmp_path, partial=True)


def test_candidate_cannot_remove_any_existing_business_key(tmp_path):
    snapshot, evidence = case(tmp_path)
    baseline = deepcopy(snapshot)
    baseline["observations"].append({"currency": "BRL", "date": "2024-01-01", "local_per_usd": 5, "provider": "BCB_SGS1"})
    with pytest.raises(FxDataError, match="removes published"):
        fx.observations(snapshot, baseline, evidence)


def test_latest_bcb_sale_and_ecb_fixing_must_equal_history(tmp_path):
    snapshot, evidence = case(tmp_path)
    evidence["raw"]["bcb_latest"] = fx._raw_record(fx.BCB_LATEST_URL,
        official_sources()(fx.BCB_LATEST_URL).replace(b"5,0000", b"5,1000"))
    with pytest.raises(FxDataError, match="sale quote differs"):
        fx.observations(snapshot, None, evidence)
    snapshot, evidence = case(tmp_path / "ecb")
    evidence["raw"]["ecb_latest"] = fx._raw_record(fx.ECB_LATEST_URL,
        official_sources()(fx.ECB_LATEST_URL).replace(b"2.5", b"2.6"))
    with pytest.raises(FxDataError, match="fixing differs"):
        fx.observations(snapshot, None, evidence)
