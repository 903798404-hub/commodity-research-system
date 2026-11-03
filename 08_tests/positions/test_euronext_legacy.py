from datetime import date

import pytest

from agri_research_agent.oilseed_positions.sources import parse_euronext, parse_euronext_legacy
from agri_research_agent.positions import delivery
from agri_research_agent.oilseed_positions.model import config
from test_positions_delivery import ROOT, STAMP


URL = 'https://live.euronext.com/sites/default/files/commodities_reporting/2022/01/12/en/cdwpr_ECO_20220112.html'


def report():
    import importlib.util
    spec = importlib.util.spec_from_file_location('euro_fixture', ROOT / '08_tests/oilseed_positions/test_oilseed_positions.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    euro = module.euro
    return euro(publication='12/01/2022').replace(b'Report date 02/10/2026', b'Report date 07/01/2022').replace(
        b'Report type COMB', b'Trading venue EURONEXT PARIS Report status NEWT')


def test_legacy_official_report_is_combined_only_and_replay_keeps_decimal_values():
    raw = report()
    rows = parse_euronext_legacy(raw, date(2022, 1, 12), URL, STAMP)
    assert len(rows) == 5 and all(r['report_type'] == 'combined' for r in rows)
    assert rows[1]['long'] == 11.25 and rows[1]['unit'] == 'delta_equivalent_contracts'
    assert rows[1]['open_interest'] is None and rows[1]['report_basis'] == 'mifid_legacy_combined_v1'
    kind, replayed = delivery._replay_source('euronext_legacy_20220112',
        dict(url=URL, retrieved_at=STAMP), raw, 'rapeseed', config(ROOT))
    assert kind == 'foreign' and replayed == rows
    with pytest.raises(ValueError, match='口径'):
        parse_euronext(raw, date(2022, 1, 12), URL, STAMP)


@pytest.mark.parametrize('mutation', ['boundary', 'wrong_url', 'unknown_type', 'explicit_combined', 'venue',
                                      'status', 'duplicate', 'different_publication', 'bad_total', 'wrong_domain'])
def test_legacy_replay_rejects_ambiguous_or_out_of_scope_source(mutation):
    raw, url, day, domain = report(), URL, date(2022, 1, 12), 'rapeseed'
    if mutation == 'boundary':
        day = date(2026, 9, 3)
        url = day.strftime('https://live.euronext.com/sites/default/files/commodities_reporting/%Y/%m/%d/en/cdwpr_ECO_%Y%m%d.html')
    if mutation == 'wrong_url': url = URL.replace('live.euronext.com', 'example.org')
    if mutation == 'unknown_type': raw = raw.replace(b'Report status NEWT', b'Report status NEWT Report type OTHER')
    if mutation == 'explicit_combined': raw = raw.replace(b'Report status NEWT', b'Report status NEWT Report type COMB')
    if mutation == 'venue': raw = raw.replace(b'EURONEXT PARIS', b'OTHER VENUE')
    if mutation == 'status': raw = raw.replace(b'Report status NEWT', b'Report status UNKNOWN')
    if mutation == 'duplicate': raw += raw
    if mutation == 'different_publication': raw = raw.replace(b'12/01/2022', b'13/01/2022')
    if mutation == 'bad_total': raw = raw.replace(b'11.25', b'12.25')
    if mutation == 'wrong_domain': domain = 'soybean'
    with pytest.raises(ValueError):
        delivery._replay_source('euronext_legacy_' + day.strftime('%Y%m%d'),
            dict(url=url, retrieved_at=STAMP), raw, domain, config(ROOT))
