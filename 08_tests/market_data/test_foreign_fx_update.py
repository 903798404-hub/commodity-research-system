from datetime import date
import hashlib
import json

import pytest

from agri_research_agent.market_data.foreign_fx import BY_CODE, FxDataError, load_update_status
from agri_research_agent.market_data.foreign_fx_update import run_update

START, END = date(2024, 1, 1), date(2024, 1, 10)


def official_sources(*, revised=False, omit_day=False, partial=False):
    days = ['2024-01-02'] if omit_day else ['2024-01-02', '2024-01-03']
    bcb = json.dumps([dict(data=date.fromisoformat(day).strftime('%d/%m/%Y'),
                           valor='5.1' if revised and day == '2024-01-02' else '5.0')
                      for day in days]).encode()
    currencies = sorted(set(BY_CODE) - {'BRL'} - ({'CAD'} if partial else set()))
    ecb = ('<Envelope><Cube>' + ''.join(
        f'<Cube time="{day}"><Cube currency="USD" rate="1.25"/>' +
        ''.join(f'<Cube currency="{code}" rate="2.5"/>' for code in currencies) + '</Cube>'
        for day in days) + '</Cube></Envelope>').encode()
    return lambda url: bcb if 'bcb.gov.br' in url else ecb


def test_update_retains_exact_raw_revisions_and_recovery_before_switch(tmp_path):
    first = run_update(tmp_path, START, END, fetcher=official_sources())
    stable = tmp_path / 'daily.json'
    original = stable.read_bytes()
    original_payload = json.loads(original)
    assert first['result'] == 'UPDATED' and first['added'] == 16
    second = run_update(tmp_path, START, END, fetcher=official_sources(revised=True))
    run = tmp_path / 'runs' / second['run_id']
    assert second['revised'] == 1 and second['added'] == 0
    assert (run / 'previous.json').read_bytes() == original
    revisions = json.loads((run / 'validation.json').read_text(encoding='utf-8'))['revisions']
    assert revisions == [dict(currency='BRL', date='2024-01-02', previous=5.0, current=5.1)]
    candidate = json.loads((run / 'candidate.json').read_text(encoding='utf-8'))
    for source in candidate['sources']:
        extension = 'json' if source['provider'] == 'BCB_SGS1' else 'xml'
        raw = (run / 'raw' / f"{source['provider']}.{extension}").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == source['raw_sha256']
    assert load_update_status(stable)['run_id'] == second['run_id']
    assert load_update_status(stable, expected_payload=original_payload) is None
    assert not (tmp_path / 'update.lock').exists()


def test_no_change_updates_check_status_without_rewriting_business_data(tmp_path):
    run_update(tmp_path, START, END, fetcher=official_sources())
    stable = tmp_path / 'daily.json'
    before, modification = stable.read_bytes(), stable.stat().st_mtime_ns
    result = run_update(tmp_path, START, END, fetcher=official_sources())
    assert result['result'] == 'NO_CHANGE'
    assert stable.read_bytes() == before and stable.stat().st_mtime_ns == modification
    assert set(result['latest_by_currency'].values()) == {'2024-01-03'}
    assert not (tmp_path / 'runs' / result['run_id'] / 'previous.json').exists()


@pytest.mark.parametrize('failure', ['network', 'parse', 'missing_day', 'partial_ecb'])
def test_failure_never_replaces_stable_or_manufactures_today(tmp_path, failure):
    run_update(tmp_path, START, END, fetcher=official_sources())
    stable = tmp_path / 'daily.json'
    before = stable.read_bytes()
    normal = official_sources(omit_day=failure == 'missing_day', partial=failure == 'partial_ecb')
    def fetcher(url):
        if 'ecb.europa.eu' in url:
            if failure == 'network':
                raise TimeoutError('source timeout')
            if failure == 'parse':
                return b'<invalid'
        return normal(url)
    with pytest.raises(Exception):
        run_update(tmp_path, START, END, fetcher=fetcher)
    assert stable.read_bytes() == before
    status = load_update_status(stable)
    assert status['result'] == 'FAILED'
    assert set(status['latest_by_currency'].values()) == {'2024-01-03'}
    run = tmp_path / 'runs' / status['run_id']
    assert (run / 'raw' / 'BCB_SGS1.json').is_file()
    assert not (tmp_path / 'update.lock').exists()


def test_contending_update_does_not_take_lock_or_overwrite_status(tmp_path):
    run_update(tmp_path, START, END, fetcher=official_sources())
    previous = (tmp_path / 'status.json').read_bytes()
    lock = tmp_path / 'update.lock'; lock.write_text('another run', encoding='utf-8')
    def forbidden(url):
        pytest.fail('contender must not fetch')
    with pytest.raises(FileExistsError):
        run_update(tmp_path, START, END, fetcher=forbidden)
    assert lock.read_text(encoding='utf-8') == 'another run'
    assert (tmp_path / 'status.json').read_bytes() == previous


def test_switch_failure_keeps_stable_and_recovery_copy(tmp_path, monkeypatch):
    from agri_research_agent.shared import atomic_storage
    run_update(tmp_path, START, END, fetcher=official_sources())
    stable = tmp_path / 'daily.json'; before = stable.read_bytes()
    replace = atomic_storage.os.replace
    def fail_stable(source, target):
        if target == stable:
            raise OSError('simulated publication failure')
        return replace(source, target)
    monkeypatch.setattr(atomic_storage.os, 'replace', fail_stable)
    with pytest.raises(OSError, match='publication failure'):
        run_update(tmp_path, START, END, fetcher=official_sources(revised=True))
    assert stable.read_bytes() == before
    status = load_update_status(stable)
    assert (tmp_path / 'runs' / status['run_id'] / 'previous.json').read_bytes() == before


def test_mismatched_or_invalid_update_status_is_not_displayed(tmp_path):
    run_update(tmp_path, START, END, fetcher=official_sources())
    stable, status_file = tmp_path / 'daily.json', tmp_path / 'status.json'
    status = json.loads(status_file.read_text(encoding='utf-8'))
    for invalid in [[], {**status, 'stable_sha256': 'a'*64}, {**status, 'checked_at': '2024-01-03T10:00:00'}]:
        status_file.write_text(json.dumps(invalid), encoding='utf-8')
        assert load_update_status(stable) is None
