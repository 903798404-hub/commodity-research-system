"""Read the website's original inputs, without collecting or changing business data.

Every adapter is isolated: one broken domain cannot hide another page's alert.
Native readers validate release identities; file mtimes are never business dates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

from .catalog import RULES
from .freshness import Observation, SHANGHAI, evaluate


@dataclass(frozen=True)
class Inputs:
    data: Path
    manual: Path
    usda: Path
    oil_world: Path
    guangxi: Path
    yunnan: Path
    source: Path


def json_file(path: Path, limit=8 * 1024**2):
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("Input identity or size invalid")
    return json.loads(path.read_text(encoding="utf-8"))


def last_date(values):
    values = list(values)
    if not values:
        return None
    parsed = pd.to_datetime(pd.Series(values), errors="raise").dropna()
    return None if parsed.empty else parsed.max().date().isoformat()


def grouped_dates(frame, groups, column):
    """Expose each series cutoff and the common cutoff, never a masking max."""
    if frame.empty:
        return Observation(None)
    dates = {str(k): last_date(v[column]) for k, v in frame.groupby(groups, dropna=False)}
    if not dates or any(v is None for v in dates.values()):
        return Observation(None, extra={"series_dates": dates})
    return Observation(min(dates.values()), extra={"series_dates": dates})


def parquet_cutoffs(path, groups, column, *, select=None, valid_value=None):
    """Bounded column projection; never materialize a full price/weather history."""
    dates = {}
    columns = list(dict.fromkeys([*groups,column]))
    predicate = None
    for key,wanted in (select or {}).items():
        expression = ds.field(key).isin(list(wanted))
        predicate = expression if predicate is None else predicate & expression
    if valid_value:
        expression = ds.field(valid_value).is_valid()
        predicate = expression if predicate is None else predicate & expression
    scanner = ds.dataset(path,format='parquet').scanner(columns=columns,filter=predicate,
        batch_size=4096,batch_readahead=0,fragment_readahead=0,use_threads=False)
    for batch in scanner.to_batches():
        table = pa.Table.from_batches([batch]).group_by(groups).aggregate([(column,'max')])
        for row in table.to_pylist():
            day = row[column + '_max']
            if day is None:
                continue
            day = date.fromisoformat(str(day)[:10]).isoformat()
            key = '/'.join(str(row[k]) for k in groups)
            dates[key] = max(day,dates.get(key,day))
    return Observation(min(dates.values()) if dates else None, extra={'series_dates':dates})


class Reader:
    def __init__(self, inputs: Inputs, now: datetime):
        self.inputs, self.now, self.cache = inputs, now, {}

    def cached(self, key, function):
        if key not in self.cache:
            try:
                self.cache[key] = function()
            except Exception as exc:
                self.cache[key] = exc
        value = self.cache[key]
        if isinstance(value, Exception):
            raise value
        return value

    def public(self):
        from agri_research_agent.market_data.activated_runtime import resolve_server_current_data_root
        return self.cached("public", lambda: resolve_server_current_data_root(self.inputs.data / "public-data-server-store"))

    def read(self, identity):
        i, root = self.inputs, self.inputs.data
        if identity in {"navigation", "monitor"}:
            return Observation(None)
        if identity == "domestic_spread":
            from agri_research_agent.application.domestic_spreads import load_domestic_spread_database, load_domestic_spread_status
            from agri_research_agent.market_data.activated_runtime import DOMESTIC_SPREAD_RELATIVE_PATH
            frame = load_domestic_spread_database(self.public() / DOMESTIC_SPREAD_RELATIVE_PATH)
            status = load_domestic_spread_status(frame)
            return Observation(status.latest_business_date,
                error=None if status.status == "success" else "正式价差合约覆盖未通过原读者校验",
                retry_safe=status.status == "success", extra=status.as_dict())
        if identity == "basis":
            from agri_research_agent.market_data.public_basis_current import load_public_basis_current
            current = load_public_basis_current(self.public() / "public-market-data/lutou-domestic-basis")
            frame = current.records
            # Historical seed is not today's quote. Keep each live series separate.
            if "segment" in frame:
                frame = frame.loc[frame.segment.isin(["LIVE_LUTOU", "LIVE_NUTSTORE"])]
            frame = frame.loc[frame.commodity.isin(['一豆','24度','三菜','豆粕','菜粕'])]
            return grouped_dates(frame, ["series_id"], "business_date")
        if identity == "sugar_basis":
            from agri_research_agent.data_sources.domestic_sugar_history import load_sugar_history
            snapshot = load_sugar_history(root / "processed/domestic_sugar_history")
            dates = {name: last_date(getattr(snapshot, name).business_date) for name in ['spot','fixed_basis','main_basis','nearest_basis']}
            return Observation(min(v for v in dates.values() if v is not None), extra={"series_dates":dates})
        if identity.startswith("fx_"):
            from agri_research_agent.market_data.foreign_fx import load_snapshot, load_update_status
            path = root / "processed/foreign_fx/daily.json"
            payload, frame = self.cached("fx", lambda: load_snapshot(path))
            status = load_update_status(path, expected_payload=payload)
            actual = last_date(frame.loc[frame.currency.eq(identity[3:]), "date"])
            # A bound successful no-change receipt proves a check, not a new date.
            checked = status.get("checked_at") if status and status.get("result") in {"UPDATED", "NO_CHANGE"} else None
            return Observation(actual, source_actual=actual if checked else None, source_checked_at=checked)
        if identity in {"fgis", "fas"}:
            from agri_research_agent.soybean_exports import fgis, fas
            from agri_research_agent.soybean_exports.research import _load_published_source
            module = fgis if identity == "fgis" else fas
            paths = getattr(module, identity + "_paths")(root.parent, "page-monitor")
            paths = {key:root / paths[key].relative_to(root.parent / '01_data') for key in ['stable','stable_manifest','status']}
            frame, manifest, status, error = _load_published_source(paths, getattr(module, "validate_" + identity + "_stable"))
            if error:
                raise ValueError("Native published export reader failed")
            actual = last_date(frame.week_ending_date)
            if manifest.get("source_latest_week") and str(manifest["source_latest_week"])[:10] != actual:
                raise ValueError("Export manifest and actual rows differ")
            return Observation(actual)
        if identity == "canola_exports":
            from agri_research_agent.canola_exports.data import load_bundle
            bundle = load_bundle(root / "processed/canola_exports/weekly.json")
            return Observation(last_date(row["week_ending"] for row in bundle["records"]))
        if identity.startswith("brazil_"):
            from agri_research_agent.pipelines.brazil_soy import load_bundle, current_season
            bundle = self.cached("brazil", lambda: load_bundle(root / "processed/brazil_soy/soy_weekly.json"))
            frame = pd.DataFrame(bundle["records"])
            frame = frame.loc[frame.season.eq(current_season(self.now.astimezone(SHANGHAI).date()))]
            metric = identity[7:]
            frame = frame.loc[~frame.metric.isin(["PLANTED", "HARVESTED"])] if metric == "growth" else frame.loc[frame.metric.eq(metric)]
            return grouped_dates(frame, ["region", "metric"], "date")
        if identity.startswith("canada_"):
            from agri_research_agent.pipelines.canada_canola import load_bundle
            bundle = self.cached("canada", lambda: load_bundle(root / "processed/canada_canola/canola_weekly.json"))
            _, province, metric = identity.split("_", 2)
            frame = pd.DataFrame(bundle["records"])
            frame = frame.loc[frame.province.eq(province) & frame.metric.eq(metric)]
            return Observation(last_date(frame.date))
        if identity.startswith("nass_"):
            metric = identity[5:]
            name = "condition" if metric == "GOOD_EXCELLENT" else "progress"
            def crop():
                folder = root / "processed/soybean_crop_progress"
                path = folder / f"soybeans_crop_{name}_weekly.parquet"
                if not path.exists():
                    path = folder / f"soybeans_crop_{name}_weekly_2021_2026.parquet"
                # A short-lived read-only observer must finish Arrow IO before
                # interpreter teardown; background prefetch can abort at exit.
                return pd.read_parquet(path,columns=['metric','geography_level','region_name','calendar_year','week_ending','value_pct'],
                                       use_threads=False,pre_buffer=False)
            frame = self.cached("nass_" + name, crop)
            frame = frame.loc[frame.geography_level.eq('US') & frame.region_name.eq("US TOTAL") & frame.calendar_year.eq(self.now.astimezone(SHANGHAI).year)]
            # The page reads the producer's already-derived GOOD_EXCELLENT.
            # Independently latest GOOD/EXCELLENT rows need not coexist in a
            # single week and cannot stand in for that published indicator.
            wanted = [metric]
            frame = frame.loc[frame.metric.isin(wanted)]
            if set(frame.metric) != set(wanted):
                return Observation(None)
            observation = grouped_dates(frame, ["metric"], "week_ending")
            complete = metric in {"PLANTED", "EMERGED", "HARVESTED"} and not frame.empty and frame.sort_values("week_ending").iloc[-1].value_pct == 100
            return Observation(observation.actual, season_complete=complete, extra=observation.extra)
        if identity.startswith("warehouse_"):
            from agri_research_agent.warehouse_receipts.model import load_snapshot
            frame, manifest = self.cached("warehouse", lambda: load_snapshot(root / "processed/warehouse_receipts", full_validation=False))
            symbol = identity[10:]
            return Observation(last_date(frame.loc[frame.symbol.eq(symbol), "date"]), extra={"snapshot_id": manifest["snapshot_id"]})
        if identity.startswith("positions_") or identity.startswith("foreign_positions_"):
            return self.positions(identity)
        if identity in {"public_market", "public_fx"}:
            from agri_research_agent.pipelines.tankan_goal_a import load_current_files
            current = self.cached("tankan", lambda: load_current_files(self.public() / "public-market-data/tankan"))
            market = identity == 'public_market'
            return parquet_cutoffs(current.directory / ('market.parquet' if market else 'fx.parquet'),
                ['series_id'],'business_date' if market else 'quote_date',select=(
                    {'exchange':{'CBOT'},'product':{'SOYBEAN'},'is_usable':{True}} if market else
                    {'base_currency':{'USD'},'quote_currency':{'CNH'},'is_usable':{True}}))
        if identity.startswith("international_"):
            from agri_research_agent.application.international_spreads import load_international_spread_public_current_table
            from agri_research_agent.research_data.three_oil_v1 import load_three_oil_v1
            oil = identity[14:]
            catalog = load_three_oil_v1()
            current = load_international_spread_public_current_table(catalog, self.public() / "public-market-data/lutou-three-oil", oil)
            frame = current.observations.select(['series_id','business_date']).to_pandas()
            return grouped_dates(frame,['series_id'],'business_date')
        if identity.startswith("weather_"):
            return self.weather(identity[8:])
        if identity in {"guangxi", "yunnan"}:
            return regional_weather(getattr(i, identity), self.now)
        if identity == "thailand":
            from agri_research_agent.weather.thailand_sugarcane import load_config, load_snapshot
            config = load_config(i.source / "02_configs/thailand_sugarcane_weather.json")
            frame, meta = load_snapshot(root / "processed/thailand_sugarcane_weather", config)
            history = frame.loc[frame.model.eq("HISTORY")]
            forecast = frame.loc[~frame.model.eq("HISTORY")]
            return weather_observation(history, forecast, "date", ["region", "model", "metric"], self.now)
        if identity in {"usda", "oil_world"}:
            return report_observation(identity, getattr(i, identity))
        return self.manual(identity)

    def positions(self, identity):
        from agri_research_agent.positions import formal_store as store
        domestic = identity.startswith("positions_")
        symbol = identity[10:] if domestic else None
        board = ({"M":"soybean","Y":"soybean","P":"palm","RM":"rapeseed","OI":"rapeseed","RS":"rapeseed","SR":"sugar"}[symbol] if domestic else 'rapeseed' if identity=='foreign_positions_euronext_rapeseed' else identity[18:])
        def cutoffs():
            formal = self.inputs.data / 'processed/commodity_positions'
            before = store.status_path(formal).read_bytes()
            status = json.loads(before)
            selected = store.pointer(formal)
            if selected is None or selected != status.get('position_store') or not status.get('published'):
                raise ValueError('Positions publication receipt is not bound to Current')
            index = store.index(formal,selected)
            seen = set()
            # The publisher validated actual rows and atomically committed their
            # latest_dates with this pointer. Authenticate Current's immutable
            # snapshot components in bounded memory, without expanding a multi-
            # gigabyte historical archive or trusting an old success timestamp.
            for name,item in index['files'].items():
                if not name.endswith('/snapshot.json'):
                    continue
                objects = [item] if item['kind']=='object' else [item['base'],*item['updates']]
                for obj in objects:
                    sha = obj['sha256']
                    if sha in seen:
                        continue
                    seen.add(sha)
                    path = formal / store.DIRECTORY / 'objects' / sha
                    if path.is_symlink() or path.stat().st_nlink != 1 or path.stat().st_size != obj['size_bytes']:
                        raise ValueError('Positions snapshot object identity invalid')
                    digest = hashlib.sha256()
                    with path.open('rb') as stream:
                        while chunk := stream.read(1024**2):
                            digest.update(chunk)
                    if digest.hexdigest() != sha:
                        raise ValueError('Positions snapshot object content differs')
            if not seen or before != store.status_path(formal).read_bytes():
                raise ValueError('Positions reader changed during check')
            return status['latest_dates'], selected['index_sha256']
        all_dates, version = self.cached('positions_dates',cutoffs)
        prefix = board + ('/domestic/' if domestic else '/foreign/')
        dates = {k[len(prefix):]:date.fromisoformat(v).isoformat() for k,v in all_dates.items()
                 if k == prefix + symbol} if domestic else {k[len(prefix):]:date.fromisoformat(v).isoformat()
                    for k,v in all_dates.items() if k.startswith(prefix)}
        if not domestic and board=='rapeseed':
            euronext = identity=='foreign_positions_euronext_rapeseed'
            dates = {k:v for k,v in dates.items() if k.startswith('euronext_rapeseed/') == euronext}
        return Observation(min(dates.values()) if dates else None, extra={'series_dates':dates,
            'date_evidence':'正式发布逐行验证回执，与当前持仓索引共同原子发布', 'index_sha256':version})

    def weather(self, identity):
        from agri_research_agent.pipelines.lutou_weather import load_weather_current
        from agri_research_agent.weather.crop_weather import load_weather_config
        current = self.cached("weather", lambda: load_weather_current(self.public() / "public-market-data/lutou-weather"))
        filenames = {'ARG':'soybean_weather_ar.yaml','AUS':'rapeseed_weather_aus.yaml','EU':'rapeseed_weather_eu.yaml',
            'RUS':'rapeseed_weather_rus.yaml','UKR':'rapeseed_weather_ukr.yaml','IND_COTTON':'cotton_weather_ind.yaml','IND_SUGARCANE':'sugarcane_weather_ind.yaml'}
        config = load_weather_config(self.inputs.source / '02_configs' / filenames[identity])
        country,crop = config['country'],config['crop']
        def cutoffs():
            return parquet_cutoffs(current.observations_path,['country','crop','region','metric'],'valid_date',
                select={'country':{'ARG','AUS','EU','RUS','UKR','IND'},'data_family':{'observation'}},valid_value='value').extra['series_dates']
        all_dates = self.cached('weather_dates',cutoffs)
        prefix = country + '/' + crop + '/'
        dates = {k:v for k,v in all_dates.items() if k.startswith(prefix)}
        regions = {row['key'] for row in config['regions']}
        required = {prefix+region+'/'+metric for region in regions for metric in ['precipitation','temperature_max']}
        missing = sorted(required - set(dates))
        def soil_dates():
            bindings = json_file(current.directory / 'soil_bindings.json')['bindings']
            bindings = [b for b in bindings if b['country'] in {'ARG','AUS','EU','RUS','UKR','IND'}]
            cutoffs = parquet_cutoffs(current.soil_observations_path,['series_id'],'business_date',
                select={'series_id':{b['series_id'] for b in bindings}},valid_value='value_percent').extra['series_dates']
            return {b['country']+'/'+b['crop']+'/'+b['region']+'/soil_moisture':cutoffs[b['series_id']]
                    for b in bindings if b['series_id'] in cutoffs}
        soil = {k:v for k,v in self.cached('weather_soil_dates',soil_dates).items() if k.startswith(prefix)}
        if config.get('enabled_sections',{}).get('soil_moisture',True):
            missing += sorted({prefix+region+'/soil_moisture' for region in regions} - set(soil))
            dates.update(soil)
        return Observation(min(dates.values()) if dates else None,
            error='原页面部分展示地区或指标没有正式记录' if missing else None,retry_safe=not missing,
            extra={'series_dates':dates,'missing_series':missing})

    def manual(self, identity):
        root = self.inputs.manual
        if identity == "sugar_history":
            from agri_research_agent.sugar_import_profit.history import load_sources
            sources, meta = load_sources(self.inputs.data / "processed/sugar_import_history")
            dates = {k: last_date(row[0] for row in v["rows"]) for k,v in sources.items()}
            return Observation(min(dates.values()), extra={"series_dates": dates})
        if identity == "manual_sugar":
            from agri_research_agent.sugar_import_profit.store import Store
            store = Store(root / "sugar-import/sugar-import.sqlite3", lambda _: (_ for _ in ()).throw(PermissionError()))
            return Observation(last_date(row["date"] for row in store.records()))
        if identity == "manual_soybean":
            from agri_research_agent.import_profit.cnf_store import load_cnf_store
            import yaml
            config = yaml.safe_load((self.inputs.source / "02_configs/import_profit_soybean.yaml").read_text(encoding="utf-8"))
            snapshot = load_cnf_store(root / "manual_cnf_quotes.parquet", allowed_origins=[r["code"] for r in config["origins"]])
            return Observation(last_date(r.business_key.business_date for r in snapshot.records if r.cnf_cents_per_bushel is not None))
        commodity = identity.rsplit("_", 1)[-1]
        path = root / "commodity-import/research.sqlite3"
        if identity.startswith("margin_history_"):
            from agri_research_agent.commodity_import_margin.history import read_history
            value, revision = read_history(path, commodity, self.now.astimezone(SHANGHAI).date())
            return Observation(last_date(r["business_date"] for r in value["rows"]) if value else None)
        if identity.startswith("margin_market_") or identity in {"manual_canola", "manual_palm"}:
            if not path.exists():
                return Observation(None)
            with sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5) as db:
                db.execute("PRAGMA query_only=ON")
                table = "market_current" if identity.startswith("margin_market_") else "cnf_day"
                rows = db.execute(f"SELECT business_date FROM {table} WHERE commodity=? ORDER BY business_date DESC LIMIT 1", (commodity,)).fetchall()
            if rows and table == "market_current":
                from agri_research_agent.commodity_import_margin.store import read_market
                read_market(path, date.fromisoformat(rows[0][0]), commodity)
            return Observation(last_date(r[0] for r in rows),
                source_actual=self.manual('manual_'+commodity).actual if table=='market_current' else None)
        raise ValueError("Missing registered observer")


def weather_observation(history, forecast, column, groups, now):
    observation = grouped_dates(history, [g for g in groups if g != "model"], column)
    dates = pd.to_datetime(forecast[column], errors="raise")
    local = now.astimezone(SHANGHAI)
    today = local.date()
    windows = {str(k): {pd.Timestamp(x).date() for x in v[column]} for k,v in forecast.groupby(groups)}
    start = None if dates.empty else dates.min().date()
    earliest = today if (local.hour,local.minute)>=(9,30) else today-timedelta(days=1)
    wanted = set() if start is None else {start+timedelta(days=n) for n in range(14)}
    complete = start is not None and earliest<=start<=today and bool(windows) and all(wanted <= days for days in windows.values())
    return Observation(observation.actual, error=None if complete else "原天气预报未覆盖今天起连续14天；历史日期仍按实际值展示",
        extra={**observation.extra, "forecast_start": None if dates.empty else dates.min().date().isoformat(), "forecast_end": None if dates.empty else dates.max().date().isoformat()})


def regional_weather(root, now):
    """Repeat native preview identities, then inspect actual regional CSV dates."""
    base = root / 'ifs_snapshot'
    raw = (base / 'manifest.json').read_bytes()
    manifest = json.loads(raw)
    def verify(folder, name, sha):
        if not isinstance(name,str) or Path(name).name != name:
            raise ValueError('Snapshot input path invalid')
        path = folder / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError('Snapshot input identity differs')
    for item in manifest['inputs']:
        verify(base,item['name'],item['sha256'])
    annual_path = root / 'ifs_history/annual_regional_daily.csv'
    verify(annual_path.parent, annual_path.name, manifest['annual_history_sha256'])
    history = pd.read_csv(annual_path)
    forecast = pd.read_csv(base / 'regional_daily_forecast.csv')
    cities = pd.read_csv(base / 'city_weights.csv')
    regions = set(history.region)
    if len(regions) != int(cities.city_weight.gt(0).sum()) + 1 or set(forecast.region) != regions or set(forecast.model) != {'EC','GFS'}:
        raise ValueError('Weather regional/model coverage differs')
    soil_root = root / 'soil_history'
    soil_status = json_file(soil_root / 'status.json')
    for name, sha in soil_status['files'].items():
        verify(soil_root, name, sha)
    if soil_status['point_weights_sha256'] != hashlib.sha256((base / 'point_weights.csv').read_bytes()).hexdigest():
        raise ValueError('Soil weather weights differ')
    soil_past = pd.read_csv(soil_root / 'annual_regional_daily.csv')
    soil_future = pd.read_csv(soil_root / 'forecast_regional_daily.csv')
    if set(soil_past.region) != regions or set(soil_future.region) != regions:
        raise ValueError('Soil regional coverage differs')
    past = grouped_dates(history, ['region'], 'date')
    soil = grouped_dates(soil_past, ['region'], 'date')
    forecast = forecast.rename(columns={'valid_date_bjt':'date'})
    weather = weather_observation(history,forecast,'date',['region','model'],now)
    start = date.fromisoformat(weather.extra['forecast_start'])
    wanted = {start + timedelta(days=n) for n in range(14)}
    soil_complete = all(wanted <= {date.fromisoformat(str(x)[:10]) for x in part.date} for _,part in soil_future.groupby('region'))
    if raw != (base / 'manifest.json').read_bytes():
        raise ValueError('Weather reader changed during check')
    return Observation(min(past.actual,soil.actual),error=weather.error if soil_complete else '土壤预报未覆盖今天起连续14天',
        extra={'history_date':past.actual,'soil_history_date':soil.actual,'forecast_run':manifest['forecast_run_utc'],
            'forecast_start':weather.extra['forecast_start'],'forecast_end':weather.extra['forecast_end'],'snapshot_id':manifest['snapshot_id']})


def report_observation(identity, root):
    if identity == "usda":
        pointer = json_file(root / "current.json")
        release = pointer["release_id"]
        if not isinstance(release, str) or Path(release).name != release:
            raise ValueError("Report release invalid")
        version = json_file(root / "releases" / release / "data/report_version.json")
        if version["currentReportMonth"] != pointer["report_month"]:
            raise ValueError("Report month and reader differ")
        return Observation(version["currentReportMonth"])
    pointer = json_file(root / "latest.json")
    releases = json_file(root / "releases.json")
    selected = [r for r in releases["releases"] if r["release"] == pointer["release"] and r["available"] and r["quality_status"] == "passed"]
    if len(selected) != 1:
        raise ValueError("Oil World current release unavailable")
    return Observation(selected[0]["release"])


def collect(inputs: Inputs, now: datetime, identities=None):
    reader = Reader(inputs, now)
    rules = [rule for rule in RULES if identities is None or rule.identity in identities]
    trading_days = []
    try:
        if any(rule.cadence=='china_daily' for rule in rules):
            from akshare.futures.cons import get_calendar
            trading_days = [date.fromisoformat(str(v)[:10]) for v in get_calendar()]
    except Exception:
        trading_days = []
    rows = []
    for rule in rules:
        if rule.excluded:
            observation = Observation(None)
        else:
            try:
                print('page_monitor_read ' + rule.identity, file=sys.stderr, flush=True)
                observation = reader.read(rule.identity)
            except FileNotFoundError:
                observation = Observation(None, error="网页原输入文件缺失；等待原更新器或原资料交付", retry_safe=bool(rule.job))
            except Exception:
                # Detailed diagnostic stays in the private operation evidence;
                # the public page contains no credentials or internal paths.
                import traceback
                print(rule.identity + ": " + traceback.format_exc(), file=sys.stderr)
                observation = Observation(None, error="原读者或文件身份校验失败，已停止该项自动重试", retry_safe=False)
        row = evaluate(rule, observation, now, trading_days)
        row["retry_blocked_reason"] = ("" if row["retry_eligible"] else "原人工资料或录价，需按原流程交付" if rule.manual else "该来源没有已授权的独立服务器更新器" if not rule.job and rule.cadence != "static" and not rule.excluded else "")
        rows.append(row)
    return rows
