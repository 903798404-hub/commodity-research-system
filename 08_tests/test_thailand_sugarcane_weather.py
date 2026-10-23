from __future__ import annotations
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "05_apps"))
from agri_research_agent.weather import thailand_sugarcane as weather

CONFIG = ROOT / "02_configs/thailand_sugarcane_weather.json"


def test_official_area_reconciles_and_coverage_is_not_renormalized():
    config = weather.load_config(CONFIG)
    assert len(config["all_province_areas"]) == 47
    assert sum(int(r["area_2025_rai"]) for r in config["all_province_areas"]) == 9628085
    assert sum(r["area_rai"] for r in config["regions"]) == 7476274
    assert sum(r["national_share"] for r in config["regions"]) == pytest.approx(0.7765068546860565)
    assert all(r["verified"] and r["locality_source_period"] and r["coordinate_source"] for r in config["regions"])


def test_tampered_weights_are_rejected(tmp_path):
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    config["regions"][0]["national_share"] = .9
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="面积"):
        weather.load_config(path)


def hourly_payload(*, missing_rain=False, missing_temp=False):
    times = pd.date_range("2020-01-01", "2020-01-03", freq="h")
    rain = list(range(len(times)))
    temp = [0.] * len(times)
    if missing_rain:
        rain[7] = None
    if missing_temp:
        temp[0] = None
    return [dict(latitude=16., longitude=101., utc_offset_seconds=25200,
                 hourly_units={"precipitation":"mm", "temperature_2m":"°C", **{k:"m³/m³" for k in weather.SOIL}},
                 hourly={"time":times.strftime("%Y-%m-%dT%H:%M").tolist(), "precipitation":rain,
                         "temperature_2m":temp, **{k:[v]*len(times) for k,v in zip(weather.SOIL,[.1,.2,.3])}})]


def test_thai_rain_interval_temperature_completeness_soil_units_and_zero():
    regions = [dict(key="test", latitude=16., longitude=101.)]
    records = weather.hourly_to_daily(hourly_payload(), regions, "2020-01-01", "2020-01-02",
                                     model="HISTORY", run_at=None, fetched_at="2020-01-04T00:00:00Z")
    assert records[records.metric.eq("precipitation")].value.tolist() == [300., 876.]
    assert records[records.metric.eq("temperature_max")].value.tolist() == [0., 0.]
    assert not records.metric.eq("soil_moisture").any()
    assert set(records.data_type) == {"historical_model"}
    missing = weather.hourly_to_daily(hourly_payload(missing_rain=True, missing_temp=True), regions, "2020-01-01", "2020-01-02",
                                     model="HISTORY", run_at=None, fetched_at="2020-01-04T00:00:00Z")
    assert missing.loc[missing.date.eq(pd.Timestamp("2020-01-01")) & missing.metric.isin(["precipitation","temperature_max"]), "value"].isna().all()
    assert missing.loc[missing.date.eq(pd.Timestamp("2020-01-02")), "value"].notna().all()


def test_era5_soil_uses_all_three_complete_layers_and_preserves_missing_and_zero():
    regions = [dict(key="test", latitude=16., longitude=101.)]
    payload = hourly_payload()
    for key in ("precipitation", "temperature_2m"):
        del payload[0]["hourly"][key]
        del payload[0]["hourly_units"][key]
    args = dict(regions=regions, start="2020-01-01", end="2020-01-02", model="HISTORY",
                source_model="era5_land", run_at=None, fetched_at="2020-01-04T00:00:00Z")
    records = weather.hourly_to_daily(payload, **args)
    assert records.value.tolist() == pytest.approx([26.5, 26.5])
    assert set(records.source_model) == {"era5_land"}
    assert set(records.source_kind) == set(records.data_type) == {"reanalysis"}
    payload[0]["hourly"][weather.SOIL[2]][7] = None
    for key in weather.SOIL:
        payload[0]["hourly"][key][24:48] = [0.] * 24
    records = weather.hourly_to_daily(payload, **args)
    assert pd.isna(records.value.iloc[0]) and records.value.iloc[1] == 0


def test_wrong_timezone_or_returned_coordinate_is_rejected():
    args = dict(regions=[dict(key="test",latitude=16.,longitude=101.)],start="2020-01-01",end="2020-01-02",model="HISTORY",run_at=None,fetched_at="2020-01-04T00:00:00Z")
    payload = hourly_payload()
    payload[0]["utc_offset_seconds"] = 28800
    with pytest.raises(ValueError, match="UTC"):
        weather.hourly_to_daily(payload, **args)
    payload = hourly_payload()
    payload[0]["longitude"] = 108
    with pytest.raises(ValueError, match="位置"):
        weather.hourly_to_daily(payload, **args)


def fixture_records(config, *, small=False):
    rows = []
    history_dates = [pd.Timestamp("2020-01-01")] if small else [pd.Timestamp(y,1,1) for y in range(2020,2027)] + [pd.Timestamp("2026-10-20")]
    start = pd.Timestamp("2020-01-02" if small else "2026-10-21")
    run = (start-pd.Timedelta(days=1)).tz_localize("UTC") + pd.Timedelta(hours=12)
    for region in config["regions"]:
        for model, dates, metrics in (("HISTORY",history_dates,weather.METRICS), ("ECMWF",pd.date_range(start,periods=14),["precipitation","temperature_max"]), ("GFS",pd.date_range(start,periods=14),["precipitation","temperature_max"])):
            for date in dates:
                for metric in metrics:
                    rows.append(dict(date=date,region=region["key"],metric=metric,model=model,
                                     forecast_run_at=None if model=="HISTORY" else run,
                                     retrieved_at_utc="2026-10-21T00:00:00Z",value=1.,unit=weather.METRICS[metric],
                                     data_type="reanalysis" if metric=="soil_moisture" else "historical_model" if model=="HISTORY" else "forecast",
                                     source_kind="reanalysis" if metric=="soil_moisture" else "historical_model" if model=="HISTORY" else "forecast",
                                     source_model="era5_land" if metric=="soil_moisture" else "ecmwf_ifs" if model=="HISTORY" else "ecmwf_ifs025" if model=="ECMWF" else "gfs_global"))
    return pd.DataFrame(rows)


def make_snapshot(root, *, small=False):
    config = weather.load_config(CONFIG)
    records = fixture_records(config,small=small)
    release = root / "releases" / "test"
    release.mkdir(parents=True)
    records.to_parquet(release / "records.parquet",index=False)
    start = pd.Timestamp("2020-01-02" if small else "2026-10-21")
    run = (start-pd.Timedelta(days=1)).tz_localize("UTC")+pd.Timedelta(hours=12)
    meta = dict(schema_version=weather.SNAPSHOT_SCHEMA, soil_source_model="era5_land", soil_history_end="2020-01-01" if small else "2026-10-20", release_id="test",config_sha256=config["config_sha256"], records_sha256=weather.sha256(release/"records.parquet"),
                history_start="2020-01-01",history_end=str((start-pd.Timedelta(days=1)).date()),
                forecast_start=str(start.date()),forecast_end=str((start+pd.Timedelta(days=13)).date()),
                forecast_run_at=run.isoformat(),published_at_utc="2026-10-21T00:00:00Z",null_history_values=0)
    weather.atomic_json(release/"manifest.json",meta)
    weather.atomic_json(root/"current.json",dict(release_id="test",manifest_sha256=weather.sha256(release/"manifest.json")))
    return config


class NoNetwork:
    def get(self, *args, **kwargs):
        raise AssertionError("Unexpected network request")


def test_duplicate_update_is_zero_network_and_preserves_bytes(tmp_path):
    make_snapshot(tmp_path,small=True)
    before = {str(f.relative_to(tmp_path)):f.read_bytes() for f in tmp_path.rglob("*") if f.is_file()}
    result = weather.refresh(tmp_path,CONFIG,publish=True,now=datetime(2020,1,2,3,tzinfo=timezone.utc),session=NoNetwork())
    assert result["status"] == "SKIPPED_ALREADY_CURRENT" and result["requests"] == 0 and result["publications"] == 0
    assert all((tmp_path/p).read_bytes()==b for p,b in before.items())


def test_failed_update_retains_old_pointer_and_records(tmp_path):
    make_snapshot(tmp_path,small=True)
    pointer = (tmp_path/"current.json").read_bytes()
    data = (tmp_path/"releases/test/records.parquet").read_bytes()
    with pytest.raises(AssertionError,match="network"):
        weather.refresh(tmp_path,CONFIG,publish=True,now=datetime(2020,1,3,3,tzinfo=timezone.utc),session=NoNetwork())
    assert (tmp_path/"current.json").read_bytes()==pointer
    assert (tmp_path/"releases/test/records.parquet").read_bytes()==data


def test_snapshot_tamper_and_traversal_are_rejected(tmp_path):
    config=make_snapshot(tmp_path)
    data=tmp_path/"releases/test/records.parquet"
    data.write_bytes(data.read_bytes()+b"tamper")
    with pytest.raises(ValueError,match="校验"):
        weather.load_snapshot(tmp_path,config)
    weather.atomic_json(tmp_path/"current.json",dict(release_id="../outside",manifest_sha256="x"))
    with pytest.raises(ValueError,match="非法"):
        weather.load_snapshot(tmp_path,config)


def test_forecasts_require_complete_same_run_windows_and_preserve_valid_zero():
    config=weather.load_config(CONFIG)
    records=fixture_records(config,small=True)
    records.loc[records.model.eq("ECMWF"),"value"]=0.
    weather.validate_records(records,config,forecast_start="2020-01-02",forecast_end="2020-01-15")
    incomplete=records.drop(records[records.model.eq("GFS")].index[-1])
    with pytest.raises(ValueError,match="窗口不完整"):
        weather.validate_records(incomplete,config,forecast_start="2020-01-02",forecast_end="2020-01-15")
    records.loc[records.model.eq("GFS"),"forecast_run_at"]=pd.Timestamp("2020-01-01T00:00:00Z")
    with pytest.raises(ValueError,match="共同起报"):
        weather.validate_records(records,config,forecast_start="2020-01-02",forecast_end="2020-01-15")


def test_cumulative_rain_and_baseline_never_bridge_missing_values():
    page=importlib.import_module("thailand_sugarcane_weather_page")
    rows=[]
    for year in range(2020,2027):
        for day in (1,2,3):
            rows.append(dict(date=pd.Timestamp(year,1,day),region="a",metric="precipitation",model="HISTORY",value=np.nan if year in (2022,2026) and day==2 else 1.))
    records=pd.DataFrame(rows)
    fig=page.seasonal_figure(records,"a","precipitation",cumulative=True,current_year=2026)
    current=next(t for t in fig.data if t.name=="2026")
    assert current.y[0]==1 and pd.isna(current.y[1]) and pd.isna(current.y[2])
    normal=next(t for t in fig.data if t.name=="2020—2024年均值")
    assert pd.isna(normal.y[1]) and pd.isna(normal.y[2])


def test_soil_baseline_is_recalculated_only_from_era5_and_requires_five_values():
    page=importlib.import_module("thailand_sugarcane_weather_page")
    rows=[]
    for year in range(2020,2027):
        for day in (1,2,3):
            rows.append(dict(date=pd.Timestamp(year,1,day),region="a",metric="soil_moisture",model="HISTORY",
                             source_model="era5_land", source_kind="reanalysis",value=np.nan if year==2022 and day==2 else year-2000.))
    # IFS-only dates must not enter either a historical curve or the five-year mean.
    rows.append(dict(date=pd.Timestamp(2025,1,4),region="a",metric="soil_moisture",model="HISTORY",
                     source_model="ecmwf_ifs",source_kind="historical_model",value=99.))
    fig=page.seasonal_figure(pd.DataFrame(rows),"a","soil_moisture",cumulative=False,current_year=2026)
    normal=next(t for t in fig.data if t.name=="2020—2024年均值")
    assert normal.y[0]==22. and pd.isna(normal.y[1]) and normal.y[2]==22.
    assert all(not t.connectgaps for t in fig.data)
    previous=next(t for t in fig.data if t.name=="2025")
    assert len(previous.y)==3 and 99. not in previous.y


def test_v2_rejects_any_ifs_soil_identity():
    config=weather.load_config(CONFIG)
    records=fixture_records(config,small=True)
    records.loc[records.metric.eq("soil_moisture"),"source_model"]="ecmwf_ifs"
    with pytest.raises(ValueError,match="ERA5-Land"):
        weather.validate_records(records,config,require_era5_land=True)


def test_legacy_migration_recomputes_all_regions_and_latency_does_not_block_update(tmp_path,monkeypatch):
    config=make_snapshot(tmp_path,small=True)
    release=tmp_path/"releases/test"
    old=pd.read_parquet(release/"records.parquet")
    old.loc[old.metric.eq("soil_moisture"),["source_model","source_kind","data_type"]]=["ecmwf_ifs","historical_model","historical_model"]
    old.to_parquet(release/"records.parquet",index=False)
    meta=json.loads((release/"manifest.json").read_text())
    meta["schema_version"]="thailand-sugarcane-snapshot/1"
    meta.pop("soil_source_model")
    meta["records_sha256"]=weather.sha256(release/"records.parquet")
    weather.atomic_json(release/"manifest.json",meta)
    weather.atomic_json(tmp_path/"current.json",dict(release_id="test",manifest_sha256=weather.sha256(release/"manifest.json")))
    before=(release/"records.parquet").read_bytes()
    calls=[]
    def fake_fetch(root,endpoint,params,session,stats):
        calls.append(params.copy());stats["requests"]+=1
        start=params.get("start_date","2020-01-03")
        end=params.get("end_date","2020-01-17")
        dates=pd.date_range(start,pd.Timestamp(end)+pd.Timedelta(hours=23),freq="h")
        soil=params["models"]=="era5_land"
        items=[]
        for number,r in enumerate(config["regions"]):
            hourly={"time":dates.strftime("%Y-%m-%dT%H:%M").tolist()}
            for k in params["hourly"].split(","):
                hourly[k]=[None if soil and d.date()==pd.Timestamp("2020-01-02").date() else .2 if soil else 1. for d in dates]
            items.append(dict(latitude=r["latitude"],longitude=r["longitude"],location_id=number,utc_offset_seconds=25200,
                              hourly_units={k:"m³/m³" if soil else "mm" if k=="precipitation" else "°C" for k in hourly if k!="time"},hourly=hourly))
        return items,dict(downloaded_at_utc="2020-01-03T03:00:00Z")
    monkeypatch.setattr(weather,"fetch_hourly",fake_fetch)
    now=datetime(2020,1,3,3,tzinfo=timezone.utc)
    result=weather.refresh(tmp_path,CONFIG,publish=True,now=now)
    assert result["status"]=="PUBLISHED"
    records,meta=weather.load_snapshot(tmp_path,config)
    soil=records[records.metric.eq("soil_moisture")]
    assert len(soil)==32 and set(soil.source_model)=={"era5_land"}
    assert soil[soil.date.eq(pd.Timestamp("2020-01-02"))].value.isna().all()
    assert meta["history_end"]=="2020-01-02" and meta["soil_history_end"]=="2020-01-01"
    assert len(meta["soil_region_latest_valid"])==16
    assert next(p for p in calls if p["models"]=="era5_land")["start_date"]=="2020-01-01"
    assert (release/"records.parquet").read_bytes()==before
    preserved=records[records.model.eq("HISTORY")&records.metric.ne("soil_moisture")&records.date.eq(pd.Timestamp("2020-01-01"))]
    assert preserved.value.eq(1.).all()
    assert weather.refresh(tmp_path,CONFIG,publish=True,now=now,session=NoNetwork())["status"]=="SKIPPED_ALREADY_CURRENT"


def test_native_main_route_four_sections_tables_and_filters_without_network(tmp_path,monkeypatch):
    make_snapshot(tmp_path)
    monkeypatch.setenv("THAILAND_SUGARCANE_WEATHER_ROOT",str(tmp_path))
    monkeypatch.setattr(weather.requests.Session,"get",NoNetwork().get)
    app=AppTest.from_file(str(ROOT/"05_apps/streamlit_app.py"))
    app.query_params["workspace_page"]="泰国甘蔗天气"
    app.run(timeout=45)
    assert not app.exception and not app.error
    assert not app.get("iframe")
    assert len(app.get("plotly_chart"))==16 and len(app.dataframe)==2
    for section in ("单日降雨","最高气温","土壤墒情"):
        next(r for r in app.radio if r.label=="页面章节").set_value(section).run(timeout=45)
        assert not app.exception and not app.error
        assert len(app.get("plotly_chart"))==16
    app.multiselect[0].set_value(["udonthani"]).run(timeout=45)
    assert len(app.get("plotly_chart"))==1 and not app.exception


def test_missing_snapshot_explains_unavailable_and_does_not_render_fake_weather(tmp_path,monkeypatch):
    monkeypatch.setenv("THAILAND_SUGARCANE_WEATHER_ROOT",str(tmp_path))
    app=AppTest.from_file(str(ROOT/"05_apps/streamlit_app.py"))
    app.query_params["workspace_page"]="泰国甘蔗天气"
    app.run(timeout=45)
    assert not app.exception and app.error
    assert not app.get("plotly_chart") and len(app.dataframe)==1
