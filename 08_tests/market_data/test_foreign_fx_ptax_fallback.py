"""Official same-fixing fallback must retain exact SGS history and replay offline."""
from copy import deepcopy
from datetime import date, datetime
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from agri_research_agent.market_data import foreign_fx_delivery as fx
from agri_research_agent.market_data.foreign_fx import FxDataError
from test_foreign_fx_incremental import baseline, feed


def fallback_feed(*, revision=False, drop=False, next_page=False):
    regular, calls = feed()
    def fetch(url):
        calls.append(url)
        if url.startswith('https://api.bcb.gov.br/'):
            return b'<?xml version="1.0"?><html><title>Bad request</title></html>'
        if url.startswith(fx.PTAX_HISTORY_BASE):
            query = parse_qs(urlsplit(url).query)
            start = datetime.strptime(query['@dataInicial'][0], "'%m-%d-%Y'").date()
            end = datetime.strptime(query['@dataFinalCotacao'][0], "'%m-%d-%Y'").date()
            days = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 5)]
            rows = [dict(cotacaoCompra=4.9994, cotacaoVenda=5.1 if revision and d.day == 2 else 5.0,
                         dataHoraCotacao=d.isoformat()+' 13:04:14.548156')
                    for d in days if start <= d <= end and not (drop and d.day == 3)]
            result = {'@odata.context':'https://was-p.bcnet.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata$metadata#_CotacaoDolarPeriodo','value':rows}
            if next_page: result['@odata.nextLink']='https://example.com/next'
            return json.dumps(result).encode()
        return regular(url)
    return fetch, calls


def test_failed_sgs_uses_ptax_only_with_exact_overlap_and_replays_mixed_archive(tmp_path):
    old, evidence = baseline(tmp_path)
    before = deepcopy((old, evidence))
    fetch, calls = fallback_feed()
    candidate, proof = fx.collect_incremental(date(2024, 1, 6), tmp_path/'fallback',
        baseline=old, baseline_evidence=evidence, fetcher=fetch)
    result = fx.observations(candidate, old, proof)
    assert result['added'] == 8 and result['revised'] == 0
    assert (old, evidence) == before
    assert proof['segments'][0]['raw']['bcb_history'] == evidence['raw']['bcb_history']
    assert proof['segments'][-1]['raw']['bcb_history']['url'].startswith(fx.PTAX_HISTORY_BASE)
    by_key = {(r['currency'], r['date']):r for r in candidate['observations']}
    assert all(by_key[(r['currency'],r['date'])] == r for r in old['observations'])
    selection = json.loads((tmp_path/'fallback/bcb_source_selection.json').read_bytes())
    assert selection['overlap_dates_checked'] == 2 and selection['overlap_changed'] == 0
    assert (tmp_path/'fallback/bcb_history_primary_failed.raw').read_bytes().startswith(b'<?xml')
    assert sum(u.startswith(fx.PTAX_HISTORY_BASE) for u in calls) == 1
    assert fx.replay(candidate, proof)[1]['BRL'] == '2024-01-05'


@pytest.mark.parametrize('options', [{'revision':True},{'drop':True},{'next_page':True}])
def test_fallback_cannot_rewrite_or_omit_sgs_history_or_hide_pagination(tmp_path, options):
    old, evidence = baseline(tmp_path); before = deepcopy((old, evidence))
    fetch, _ = fallback_feed(**options)
    with pytest.raises(FxDataError):
        fx.collect_incremental(date(2024,1,6), tmp_path/'rejected', baseline=old,
            baseline_evidence=evidence, fetcher=fetch)
    assert (old,evidence) == before and not (tmp_path/'rejected/daily.json').exists()


def test_monthly_full_reconciliation_fallback_matches_entire_published_brl_history(tmp_path):
    old, evidence = baseline(tmp_path); fetch, _ = fallback_feed()
    candidate, proof = fx.collect_incremental(date(2024,2,3), tmp_path/'monthly',
        baseline=old, baseline_evidence=evidence, fetcher=fetch)
    assert proof['schema_version'] == fx.EVIDENCE_SCHEMA
    assert proof['raw']['bcb_history']['url'] == fx.ptax_url(fx.START,date(2024,2,3))
    assert fx.observations(candidate, old, proof)['revised'] == 0


def test_unverified_initialization_cannot_use_fallback_and_mismatched_url_rejected(tmp_path):
    fetch, calls = fallback_feed()
    with pytest.raises((FxDataError, json.JSONDecodeError)):
        fx.collect(date(2024,1,6), tmp_path/'init', fetcher=fetch)
    assert not any(u.startswith(fx.PTAX_HISTORY_BASE) for u in calls)
    record=fx._raw_record(fx.ptax_url(date(2024,1,2),date(2024,1,6)),b'{}')
    with pytest.raises(FxDataError,match='URL or date window'):
        fx._bcb_capture(record, date(2024,1,3),date(2024,1,6))


@pytest.mark.parametrize('bad', ['duplicate','missing_sale','zero','future','intraday_endpoint'])
def test_ptax_rejects_ambiguous_or_wrong_quote(bad):
    raw={'@odata.context':'https://was-p.bcnet.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata$metadata#_CotacaoDolarPeriodo',
         'value':[{'cotacaoCompra':4.9994,'cotacaoVenda':5.0,'dataHoraCotacao':'2024-01-05 13:00:00'}]}
    if bad=='duplicate':raw['value'].append(deepcopy(raw['value'][0]))
    elif bad=='missing_sale':raw['value'][0].pop('cotacaoVenda')
    elif bad=='zero':raw['value'][0]['cotacaoVenda']=0
    elif bad=='future':raw['value'][0]['dataHoraCotacao']='2024-01-07 13:00:00'
    else:raw['@odata.context']=raw['@odata.context'].replace('CotacaoDolarPeriodo','CotacaoMoedaPeriodo')
    with pytest.raises(FxDataError):fx.parse_ptax(json.dumps(raw).encode(),date(2024,1,2),date(2024,1,6))


def test_duplicate_json_keys_and_future_full_request_fail_before_collection(tmp_path):
    with pytest.raises(FxDataError, match='duplicate JSON'):
        fx.parse_ptax(b'{"value":[],"value":[]}',date(2024,1,2),date(2024,1,6))
    with pytest.raises(FxDataError, match='request date'):
        fx.collect(date(9999,1,1),tmp_path/'invalid',fetcher=lambda _:pytest.fail('future request'))
