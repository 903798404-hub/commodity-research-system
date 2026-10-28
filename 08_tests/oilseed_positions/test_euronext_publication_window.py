from datetime import date, datetime
from unittest.mock import Mock

import pytest
import requests

from agri_research_agent.oilseed_positions.sources import Sources, SourceNotPublished


@pytest.mark.parametrize('clock,published',[
    ('2026-10-27T23:45:00+00:00','2026-10-28'),
    ('2026-10-28T10:59:59+00:00','2026-10-28'),
    ('2026-07-22T09:59:59+00:00','2026-07-22'),
    ('2026-10-28T12:00:00+00:00','2026-11-04'),
])
def test_unpublished_window_does_not_request_future_report(clock,published):
    source=Sources(clock=lambda:datetime.fromisoformat(clock))
    source._response=Mock(side_effect=AssertionError('future report must not be requested'))
    with pytest.raises(SourceNotPublished):source.euronext(date.fromisoformat(published))
    source._response.assert_not_called()


@pytest.mark.parametrize('clock,published',[
    ('2026-10-28T11:00:00+00:00','2026-10-28'),
    ('2026-07-22T10:00:00+00:00','2026-07-22'),
    ('2026-10-28T01:00:00+00:00','2026-10-21'),
    ('2026-10-29T00:00:00+00:00','2026-10-28'),
])
def test_eligible_or_historical_report_403_remains_real_failure(clock,published):
    source=Sources(clock=lambda:datetime.fromisoformat(clock))
    error=requests.HTTPError('403 for eligible report')
    source._response=Mock(side_effect=error)
    with pytest.raises(requests.HTTPError) as caught:source.euronext(date.fromisoformat(published))
    assert caught.value is error
    source._response.assert_called_once()
    assert f'cdwpr_ECO_{date.fromisoformat(published):%Y%m%d}.html' in source._response.call_args.args[1]


def test_unknown_clock_fails_closed():
    source=Sources(clock=lambda:datetime(2026,10,28))
    source._response=Mock()
    with pytest.raises(ValueError,match='timezone aware'):source.euronext(date(2026,10,28))
    source._response.assert_not_called()
