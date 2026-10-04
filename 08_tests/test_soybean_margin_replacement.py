from datetime import date
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd
import pytest

from agri_research_agent.soybean_margin.model import (
    calculate, contracts, daily_rows, FIELDS, history_matrix, read_history,
    resolve_history, seasonal_series, overlay_cnf, number,
)
from agri_research_agent.soybean_margin.store import load, save, read_all
from agri_research_agent.soybean_margin.charts import build_margin_charts, _pm_seasonal_figure

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "05_apps"))
from soybean_margin_page import daily_html, _overlay_manual


def source(day=date(2026,6,25), cnf=0., **changes):
    row = dict(business_date=day,commodity="soybean",origin="brazil",shipment_year=2026,
               shipment_month=9,cnf_cents_per_bushel=cnf,cbot_contract_year=2026,
               cbot_contract_month=9,cbot_price_cents_per_bushel=1000.,fx_value=7.,
               soymeal_contract_code="M2701",soyoil_contract_code="Y2701",
               soymeal_price_cny_per_tonne=3000.,soyoil_price_cny_per_tonne=8000.)
    row.update(changes)
    return pd.DataFrame([row])


def test_zero_cnf_is_valid_and_net_margin_deducts_both_fees():
    result = calculate(0,1000,7,3000,8000)
    cost = 1000*.367437*7*1.03*1.09
    assert result["duty_paid_cost"] == pytest.approx(cost)
    assert result["net_margin"] == pytest.approx(3000*.795+8000*.19-cost-200)


@pytest.mark.parametrize("index,value", [(0,None),(1,None),(1,0),(2,0),(2,float('nan')),(3,None),(4,float('inf'))])
def test_missing_or_invalid_inputs_do_not_generate_profit(index,value):
    values = [0,1000,7,3000,8000]
    values[index] = value
    assert calculate(*values)["net_margin"] is None


def test_mapping_uses_full_year_and_keeps_existing_policy():
    assert contracts(date(2026,10,4),12) == ('2701','2701',2026)
    assert contracts(date(2026,10,4),1) == ('2701','2705',2027)
    assert contracts(date(2026,6,25),9) == ('2609','2701',2026)
    assert contracts(date(2026,6,25),8) == ('2609','2605',2026)


def test_twelve_rows_keep_empty_dates_empty():
    rows = daily_rows(source(),date(2026,9,30),'brazil')
    assert len(rows)==12
    assert [r['shipment_month'] for r in rows] == list(range(1,13))
    assert all(r['net_margin'] is None for r in rows)
    html = daily_html(rows, '巴西', date(2026,9,30))
    assert html.count('class="profit-row"')==12
    assert '79.5%' in html and '150元' in html


def test_manual_zero_and_clear_have_different_meanings(tmp_path):
    db=tmp_path/'cnf.sqlite3'
    day=date(2026,9,30)
    values={m: None for m in range(1,13)}
    values[9]=0
    assert load(db,day,'brazil')==({},0)
    assert not db.exists()
    assert save(db,day,'brazil',values,0)==1
    assert load(db,day,'brazil')[0][9]==0
    assert load(db,day,'us_gulf')==({},0)
    with pytest.raises(ValueError,match='另一会话'):
        save(db,day,'brazil',values,0)
    values[9]=None
    save(db,day,'brazil',values,1)
    assert load(db,day,'brazil')[0][9] is None


def test_invalid_save_leaves_no_database(tmp_path):
    db=tmp_path/'cnf.sqlite3'
    with pytest.raises(ValueError):
        save(db,date(2026,9,30),'brazil',{m:float('nan') for m in range(1,13)},0)
    assert not db.exists()


def test_manual_current_date_survives_without_market_history(tmp_path):
    day=date(2026,9,30)
    path=tmp_path/'manual.parquet'
    pd.DataFrame([dict(business_date=day,origin='brazil',shipment_year=2027,
                       shipment_month=1,cnf_cents_per_bushel=0)]).to_parquet(path)
    data=_overlay_manual(source(),str(path))
    row=daily_rows(data,day,'brazil')[0]
    assert row['cnf_cents_per_bushel']==0 and row['net_margin'] is None


def test_history_keeps_workdays_and_origin_isolation():
    frame=history_matrix(source(),date(2026,6,25),'brazil',FIELDS[0])
    assert len(frame)==12 and frame.iloc[0]['9月']==0
    assert all(date.fromisoformat(d).weekday()<5 for d in frame['日期'])
    other=history_matrix(source(),date(2026,6,25),'us_gulf',FIELDS[0])
    assert other['9月'].isna().all()


def test_seasonality_has_four_month_window_and_three_year_minimum():
    data=pd.concat([source(day=date(y,6,25),shipment_year=y,cnf=float(y)) for y in range(2021,2027)],ignore_index=True)
    labels,series=seasonal_series(data,'brazil',9,FIELDS[0],date(2026,6,25))
    assert labels[0]=='05-01' and labels[-1]=='08-31'
    assert series['5年均值']['06-25']==pytest.approx(2023)
    assert series['5年均值']['06-26'] is None
    assert series['2026']['06-25']==2026


def test_history_identity_and_duplicate_rejection(tmp_path):
    release=tmp_path/'releases'/'history'
    release.mkdir(parents=True)
    path=release/'soybean_market_snapshots.parquet'
    source().to_parquet(path,index=False)
    manifest={'release_id':'history','output_files':{path.name:{'size_bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}}
    mp=release/'manifest.json'
    mp.write_text(json.dumps(manifest),encoding='utf-8')
    index={'current_release_id':'history','current_manifest_sha256':hashlib.sha256(mp.read_bytes()).hexdigest()}
    (tmp_path/'release_index.json').write_text(json.dumps(index),encoding='utf-8')
    assert resolve_history(tmp_path)[0]==path
    assert len(read_history(path))==1
    pd.concat([source(),source()]).to_parquet(path,index=False)
    with pytest.raises(ValueError,match='行情文件'):
        resolve_history(tmp_path)
    with pytest.raises(ValueError,match='重复'):
        read_history(path)


def test_original_margin_chart_format_and_cycle_axis_are_retained():
    chart=build_margin_charts(source(), 'brazil', date(2026,6,25))[8]
    figure=_pm_seasonal_figure(chart)
    assert len(build_margin_charts(source(),'brazil',date(2026,6,25)))==12
    assert figure.layout.title.text=='大豆盘面榨利：9月对09'
    assert figure.layout.height==380
    assert tuple(figure.layout.xaxis.ticktext)==tuple(f'{m}月' for m in range(1,10))
    assert figure.layout.yaxis.title.text=='盘面榨利（元/吨）'
    assert figure.data[0].mode=='lines+markers'
    assert figure.data[0].line.color=='#D62728'
    assert figure.data[0].line.width==2.8
    assert figure.data[0].connectgaps is False
    assert figure.data[0].customdata[0][0]=='2026-06-25'


def test_chart_connects_nearby_observations_without_filling_long_gaps():
    data = pd.concat([
        source(day=date(2026,6,1), retained_net_margin=100.),
        source(day=date(2026,6,2), retained_net_margin=None),
        source(day=date(2026,6,4), retained_net_margin=120.),
        source(day=date(2026,6,25), retained_net_margin=140.),
    ], ignore_index=True)
    original = data.copy(deep=True)
    figure = _pm_seasonal_figure(build_margin_charts(data, 'brazil', date(2026,6,25))[8])
    trace = figure.data[0]
    assert tuple(trace.y) == (100., 120., None, 140.)
    assert [row[0] for row in trace.customdata if row] == ['2026-06-01','2026-06-04','2026-06-25']
    assert trace.connectgaps is False
    assert trace.marker.size[0] == 0 and trace.marker.size[-1] > 0
    pd.testing.assert_frame_equal(data, original)


def test_chart_year_selection_preserves_original_observations():
    data = pd.concat([
        source(day=date(2025,6,25), shipment_year=2025, retained_net_margin=111.),
        source(day=date(2026,6,25), retained_net_margin=222.),
    ], ignore_index=True)
    chart = build_margin_charts(data, 'brazil', date(2026,6,25))[8]
    figure = _pm_seasonal_figure(chart, [2025])
    assert [trace.name for trace in figure.data] == ['2025']
    assert tuple(figure.data[0].y) == (111.,)


def test_saved_cnf_updates_history_and_chart_without_mutating_source(tmp_path):
    db = tmp_path / 'cnf.sqlite3'
    day = date(2026,6,25)
    values = {month: None for month in range(1,13)}
    values[9] = 0
    save(db, day, 'brazil', values, 0)
    original = source(cnf=100., retained_net_margin=999.)
    saved = overlay_cnf(original, read_all(db), recalculate_retained=True)
    row = saved.loc[saved.shipment_month.eq(9)].iloc[0]
    expected = calculate(0, 1000, 7, 3000, 8000)['net_margin']
    assert row.retained_net_margin == pytest.approx(expected)
    assert history_matrix(saved, day, 'brazil', FIELDS[0]).iloc[0]['9月'] == 0
    chart = build_margin_charts(saved, 'brazil', day)[8]
    assert _pm_seasonal_figure(chart).data[0].y[0] == pytest.approx(expected)
    assert original.iloc[0].retained_net_margin == 999.
    values[9] = None
    save(db, day, 'brazil', values, 1)
    cleared = overlay_cnf(original, read_all(db), recalculate_retained=True)
    assert number(cleared.loc[cleared.shipment_month.eq(9), 'retained_net_margin'].iloc[0]) is None
