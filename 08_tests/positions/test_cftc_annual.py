"""Official annual CFTC reports retain the existing API partitions and classifications."""
import csv
from datetime import date
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from agri_research_agent.positions import cftc_annual as annual
from agri_research_agent.positions import delivery
from agri_research_agent.oilseed_positions.model import config
from agri_research_agent.automation import positions_scheduled_collection as collection
from test_positions_delivery import ROOT, STAMP, archive


def packed(*, day='2026-09-29', code='080732', kind='FutOnly', name='f_year.txt', change=None):
    values={'Report_Date_as_YYYY-MM-DD':day, 'CFTC_Contract_Market_Code':code,
        'Market_and_Exchange_Names':'SUGAR NO. 11 - ICE FUTURES U.S.', 'Open_Interest_All':'100',
        'Tot_Rept_Positions_Long_All':'80','Tot_Rept_Positions_Short_All':'80',
        'FutOnly_or_Combined':kind}
    for prefix in ('Prod_Merc','Swap','M_Money','Other_Rept','NonRept'):
        for side in ('Long','Short'):
            key=f'{prefix}_Positions_{side}_All'
            if prefix=='Swap' and side=='Short':key='Swap__Positions_Short_All'
            values[key]='20'
    for key in ('Swap__Positions_Spread_All','M_Money_Positions_Spread_All','Other_Rept_Positions_Spread_All'):
        values[key]='0'
    if change:values.update(change)
    text=StringIO();writer=csv.DictWriter(text,fieldnames=values);writer.writeheader();writer.writerow(values)
    raw=BytesIO()
    with ZipFile(raw,'w',ZIP_DEFLATED) as z:z.writestr(name,text.getvalue().encode())
    return raw.getvalue()


def parse(raw, kind='futures_only'):
    return annual.parse_archive(raw,2026,kind,'sugar11','080732','SUGAR NO. 11',STAMP)


@pytest.mark.parametrize('kind,marker',[('futures_only','FutOnly'),('combined','Combined')])
def test_annual_csv_maps_all_five_categories_and_replays_offline(kind,marker):
    raw=packed(kind=marker);rows=parse(raw,kind)
    assert {row['group'] for row in rows}=={'producer','swap','managed_money','other','nonreportable'}
    assert all(row['long']==row['short']==20 and row['open_interest']==100 for row in rows)
    source={'url':annual.archive_url(2026,kind),'retrieved_at':STAMP}
    actual_kind,replayed=delivery._replay_source(f'cftc_archive_2026_sugar11_{kind}',source,raw,'sugar',config(ROOT))
    assert actual_kind=='foreign' and replayed==rows
    with pytest.raises(ValueError,match='URL invalid'):
        delivery._replay_source(f'cftc_archive_2026_sugar11_{kind}',dict(source,url='https://example.org/data.zip'),raw,'sugar',config(ROOT))
    with pytest.raises(ValueError,match='domain differs'):
        delivery._replay_source(f'cftc_archive_2026_sugar11_{kind}',source,raw,'soybean',config(ROOT))


@pytest.mark.parametrize('options',[
    {'kind':'Combined'}, {'day':'2025-09-29'}, {'code':'999999'}, {'name':'../f_year.txt'},
    {'change':{'Open_Interest_All':'101'}},
])
def test_wrong_identity_type_year_expansion_and_totals_rejected(options):
    with pytest.raises(ValueError):parse(packed(**options))


def test_existing_api_rows_are_preserved_and_revisions_fail_closed():
    old=delivery.validate_archive(archive(),ROOT)[0]['sugar']['foreign']
    same=parse(packed())
    assert annual.unseen_rows(same,old,'sugar11','futures_only',2026)==[]
    fresh=parse(packed(day='2026-10-06'))
    assert annual.unseen_rows(same+fresh,old,'sugar11','futures_only',2026)==fresh
    with pytest.raises(ValueError,match='drops existing'):
        annual.unseen_rows(fresh,old,'sugar11','futures_only',2026)
    revised=[dict(row,long=row['long']+1) for row in same]
    with pytest.raises(ValueError,match='revision requires review'):
        annual.unseen_rows(revised,old,'sugar11','futures_only',2026)
    assert all('publicreporting.cftc.gov' in row['source_url'] for row in old)


def test_server_annual_mode_skips_only_api_and_preserves_all_boards(tmp_path,monkeypatch):
    baseline=archive();calls=[]
    def collect(*args,**kwargs):
        assert kwargs['include_cftc'] is False
        return [],[],[],[{'source_id':'other_official','status':'ok'}]
    monkeypatch.setattr(collection.delivery,'_module',lambda *args:type('Collector',(),{'collect':staticmethod(collect)}))
    def annual_collect(self,domain,spec,previous,start_year):
        calls.append(domain)
        return [],[],[{'source_id':'official_annual','status':'ok'}]
    monkeypatch.setattr(annual.AnnualSources,'collect',annual_collect)
    root,attempts=collection.collect_bundle(baseline,tmp_path,markets='foreign',today=date(2026,10,9),cftc_annual=True)
    result=delivery.archive_bundle(root,ROOT)
    assert calls==['sugar','rapeseed','soybean']
    assert len(attempts)==6 and not delivery.observations(result,baseline,ROOT)['business_changed']
    with pytest.raises(ValueError,match='foreign only'):
        collection.collect_bundle(baseline,tmp_path/'domestic',markets='domestic',today=date(2026,10,9),cftc_annual=True)
