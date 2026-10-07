import pytest
from agri_research_agent.pipelines.canada_canola_update import fetch_report
from test_canada_canola import context


def test_redirect_to_nonofficial_host_is_rejected_before_request(tmp_path, monkeypatch):
    calls = []

    class Response:
        is_redirect = True
        headers = {"Location": "https://example.com/report"}

        def close(self):
            pass

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, url, **kwargs):
            calls.append(url)
            assert kwargs["allow_redirects"] is False
            return Response()

    monkeypatch.setattr("agri_research_agent.pipelines.canada_canola_update.requests.Session", Session)
    with pytest.raises(ValueError, match="official provincial"):
        fetch_report(context(tmp_path), "MB", "https://www.gov.mb.ca/report")
    assert calls == ["https://www.gov.mb.ca/report"]
    assert not list(tmp_path.rglob("report.bin"))
