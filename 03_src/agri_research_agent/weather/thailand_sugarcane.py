"""Independent Thai cane snapshots. No collection is performed by the UI."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

THAI_TZ = ZoneInfo("Asia/Bangkok")
METRICS = {"precipitation": "mm", "temperature_max": "degC", "soil_moisture": "%"}
SOIL = ("soil_moisture_0_to_7cm", "soil_moisture_7_to_28cm", "soil_moisture_28_to_100cm")
HISTORY_API = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_API = "https://single-runs-api.open-meteo.com/v1/forecast"
RELEASE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema_version") != "thailand-sugarcane-config/1":
        raise ValueError("泰国甘蔗配置版本错误")
    areas = config["all_province_areas"]
    if len(areas) != 47 or len({r["province"] for r in areas}) != 47:
        raise ValueError("泰国47省面积表存在缺失或重复")
    total = sum(int(r["area_2025_rai"]) for r in areas)
    if total != config["national_area_rai"] or total != 9628085:
        raise ValueError("省级收获面积与全国原表合计不符")
    if config["reference_area_year"] != 2025 or config["area_unit"] != "rai":
        raise ValueError("面积年度或单位错误")
    keys = []
    for r in config["regions"]:
        keys.append(r["key"])
        area = next(a for a in areas if a["province"] == r["province"])
        if (not r["verified"] or int(area["area_2025_rai"]) != r["area_rai"]
                or not math.isclose(r["national_share"], r["area_rai"] / total, abs_tol=1e-12)
                or not 5 < r["latitude"] < 21 or not 97 < r["longitude"] < 106
                or not r["locality_source"].startswith("https://")):
            raise ValueError(f"产区依据、面积或点位校验失败：{r['key']}")
    if len(keys) != len(set(keys)):
        raise ValueError("重复天气产区")
    config["config_sha256"] = sha256(path)
    return config


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def update_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "update.lock").open("a+b") as stream:
        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def fetch_hourly(root: Path, endpoint: str, params: dict, session, stats: dict | None = None) -> tuple[list[dict], dict]:
    """Cache only complete HTTP responses, identified by exact endpoint and parameters."""
    identity = hashlib.sha256(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()
    raw = root / "raw" / (identity + ".json.gz")
    receipt = raw.with_suffix(".receipt.json")
    if raw.is_file() and receipt.is_file():
        if stats is not None:
            stats["cache_hits"] += 1
        meta = json.loads(receipt.read_text(encoding="utf-8"))
        if meta["stored_sha256"] != sha256(raw):
            raise ValueError("原始天气响应缓存校验失败")
        payload = json.loads(gzip.decompress(raw.read_bytes()))
        if isinstance(payload, dict):
            payload = [payload]
        return payload, meta
    for attempt in range(5):
        if stats is not None:
            stats["requests"] += 1
        response = session.get(endpoint, params=params, timeout=(15, 150))
        if response.status_code != 429:
            break
        failures = root / "request_failures"
        failures.mkdir(parents=True, exist_ok=True)
        atomic_json(failures / f"{identity}_{attempt}.json", dict(endpoint=endpoint, parameters=params,
                    http_status=429, response=response.text[:4000], checked_at_utc=datetime.now(timezone.utc).isoformat()))
        if attempt == 4:
            break
        delay = min(60, max(1, int(response.headers.get("Retry-After", "60"))))
        print(f"API限流，保留已下载原始响应，{delay}秒后重试", flush=True)
        time.sleep(delay)
    response.raise_for_status()
    payload = response.json()
    expected = len(str(params["latitude"]).split(","))
    if expected == 1 and isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or len(payload) != expected:
        raise ValueError("天气API返回点数与请求不符")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(gzip.compress(response.content, mtime=0))
    meta = dict(endpoint=endpoint, parameters=params, response_url=response.url,
                downloaded_at_utc=datetime.now(timezone.utc).isoformat(),
                response_sha256=hashlib.sha256(response.content).hexdigest(),
                stored_sha256=sha256(raw), http_status=response.status_code)
    atomic_json(receipt, meta)
    return payload, meta


def hourly_to_daily(payload: list[dict], regions: list[dict], start, end, *, model: str,
                    run_at: str | None, fetched_at: str) -> pd.DataFrame:
    """Thai local day: rain (00:00, next 00:00], other variables [00:00, 23:00]."""
    days = pd.date_range(start, end, freq="D")
    result = []
    for number, (region, item) in enumerate(zip(regions, payload, strict=True)):
        if item.get("utc_offset_seconds") != 25200:
            raise ValueError("天气API日界不是泰国UTC+7")
        if (abs(float(item["latitude"]) - region["latitude"]) > .3
                or abs(float(item["longitude"]) - region["longitude"]) > .3
                or item.get("location_id", number) != number):
            raise ValueError("天气API位置身份或返回顺序错误")
        units = item["hourly_units"]
        if units.get("precipitation") != "mm" or units.get("temperature_2m") != "°C":
            raise ValueError("天气API降雨或温度单位变化")
        hourly = pd.DataFrame(item["hourly"]).set_index("time")
        hourly.index = pd.DatetimeIndex(hourly.index)
        if not hourly.index.is_unique or (hourly.index.minute != 0).any():
            raise ValueError("小时数据时间戳重复或未对齐")
        hours = pd.date_range(days[0], days[-1] + pd.Timedelta(days=1), freq="h")
        hourly = hourly.reindex(hours).apply(pd.to_numeric, errors="coerce")
        rain = hourly["precipitation"].copy()
        rain.index = rain.index - pd.Timedelta(hours=1)
        values = {"precipitation": rain.resample("D").sum(min_count=24).reindex(days)}
        temp = hourly["temperature_2m"].resample("D")
        values["temperature_max"] = temp.max().where(temp.count().eq(24)).reindex(days)
        if model == "HISTORY":
            if any(units.get(layer) != "m³/m³" for layer in SOIL):
                raise ValueError("土壤含水率单位变化")
            layers = hourly[list(SOIL)].resample("D")
            means = layers.mean().where(layers.count().eq(24)).reindex(days)
            values["soil_moisture"] = means.mul([.07, .21, .72]).sum(axis=1, min_count=3) * 100
        for metric, data in values.items():
            frame = pd.DataFrame({"date": days, "value": data.to_numpy()})
            frame = frame.assign(region=region["key"], metric=metric, unit=METRICS[metric],
                                 model=model, forecast_run_at=run_at, retrieved_at_utc=fetched_at,
                                 data_type="historical_model" if model == "HISTORY" else "forecast",
                                 source_model="ecmwf_ifs" if model == "HISTORY" else
                                 ("ecmwf_ifs025" if model == "ECMWF" else "gfs_global"),
                                 source_kind="historical_model" if model == "HISTORY" else "forecast",
                                 latitude=region["latitude"], longitude=region["longitude"])
            result.append(frame)
    return pd.concat(result, ignore_index=True)


def validate_records(records: pd.DataFrame, config: dict, *, history_end=None,
                     forecast_start=None, forecast_end=None) -> pd.DataFrame:
    records = records.copy()
    records["date"] = pd.to_datetime(records["date"]).dt.normalize()
    records["forecast_run_at"] = pd.to_datetime(records["forecast_run_at"], utc=True)
    if "retrieved_at_utc" not in records and "source_updated_at" in records:
        records = records.rename(columns={"source_updated_at": "retrieved_at_utc"})
    records["retrieved_at_utc"] = pd.to_datetime(records["retrieved_at_utc"], utc=True)
    keys = ["date", "region", "metric", "model", "forecast_run_at"]
    if records.duplicated(keys).any():
        raise ValueError("天气序列存在重复身份")
    if set(records["region"]) != {r["key"] for r in config["regions"]}:
        raise ValueError("天气产区覆盖与配置不符")
    if not set(records["model"]).issubset({"HISTORY", "ECMWF", "GFS"}):
        raise ValueError("出现未批准的天气模型")
    for metric, unit in METRICS.items():
        subset = records[records.metric.eq(metric)]
        if not subset.unit.eq(unit).all():
            raise ValueError("天气单位不符")
        present = subset.value.dropna()
        low, high = {"precipitation": (0, 2000), "temperature_max": (-30, 65), "soil_moisture": (0, 100)}[metric]
        if not present.between(low, high).all():
            raise ValueError(f"天气值超出物理范围：{metric}")
    if not records.loc[records.model.eq("HISTORY"), "forecast_run_at"].isna().all():
        raise ValueError("历史模式值不能伪装为预报批次")
    if records.loc[~records.model.eq("HISTORY"), "forecast_run_at"].isna().any():
        raise ValueError("预报缺少真实请求起报批次")
    if records.loc[~records.model.eq("HISTORY"), "forecast_run_at"].nunique() != 1:
        raise ValueError("EC/GFS没有使用同一共同起报批次")
    if history_end is not None:
        dates = pd.date_range("2020-01-01", history_end)
        for region in config["regions"]:
            for metric in METRICS:
                part = records[(records.region == region["key"]) & (records.metric == metric) & (records.model == "HISTORY")]
                if not pd.DatetimeIndex(part.date.sort_values()).equals(dates):
                    raise ValueError("历史每日身份不完整；不得隐藏缺日")
    if forecast_start is not None:
        dates = pd.date_range(forecast_start, forecast_end)
        for region in config["regions"]:
            for model in ("ECMWF", "GFS"):
                for metric in ("precipitation", "temperature_max"):
                    part = records[(records.region == region["key"]) & (records.metric == metric) & (records.model == model)]
                    if not pd.DatetimeIndex(part.date.sort_values()).equals(dates) or part.value.isna().any():
                        raise ValueError(f"{region['key']} {model} 预报窗口不完整")
    return records


def load_snapshot(root: Path, config: dict) -> tuple[pd.DataFrame, dict]:
    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    release_id = pointer["release_id"]
    if not isinstance(release_id, str) or not RELEASE_ID.fullmatch(release_id):
        raise ValueError("天气快照标识非法")
    release = root / "releases" / release_id
    manifest = release / "manifest.json"
    if sha256(manifest) != pointer["manifest_sha256"]:
        raise ValueError("天气快照清单校验失败")
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    if meta.get("schema_version") != "thailand-sugarcane-snapshot/1":
        raise ValueError("天气快照版本不支持")
    if meta["config_sha256"] != config["config_sha256"]:
        raise ValueError("快照产区或面积配置与页面不一致")
    records = release / "records.parquet"
    if sha256(records) != meta["records_sha256"]:
        raise ValueError("天气快照数据校验失败")
    return validate_records(pd.read_parquet(records), config), meta


def refresh(root: Path, config_path: Path, *, publish: bool = False, now=None, session=None,
            rebuild: bool = False) -> dict:
    config = load_config(config_path)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("更新时刻必须带时区")
    today = now.astimezone(THAI_TZ).date()
    history_end = today - timedelta(days=1)
    forecast_end = today + timedelta(days=13)
    # Previous 12Z is available before the inherited daily 08:30 Beijing schedule.
    run = datetime.combine(today - timedelta(days=1), datetime.min.time(), timezone.utc) + timedelta(hours=12)
    run_at = run.isoformat()
    fetched_at = now.astimezone(timezone.utc).isoformat()
    session = session or requests.Session()
    with update_lock(root):
        previous = pd.DataFrame()
        if (root / "current.json").exists():
            previous, meta = load_snapshot(root, config)
            if (not rebuild and meta["history_end"] == str(history_end) and meta["forecast_run_at"] == run_at
                    and meta["forecast_start"] == str(today) and meta["forecast_end"] == str(forecast_end)):
                return dict(status="SKIPPED_ALREADY_CURRENT", requests=0, publications=0, release_id=meta["release_id"])
        regions = config["regions"]
        common = dict(latitude=",".join(str(r["latitude"]) for r in regions),
                      longitude=",".join(str(r["longitude"]) for r in regions), timezone="Asia/Bangkok")
        history = previous[previous.model.eq("HISTORY")].copy() if not previous.empty and not rebuild else pd.DataFrame()
        if not history.empty:
            history["data_type"] = "historical_model"
        first = pd.Timestamp("2020-01-01") if history.empty else history.date.max() + pd.Timedelta(days=1)
        if not history.empty and history.value.isna().any():
            # Re-query only the suffix containing a gap; immutable prior releases remain intact.
            first = min(first, history.loc[history.value.isna(), "date"].min())
            history = history[history.date < first]
        chunks = [history] if not history.empty else []
        downloads = 0
        stats = dict(requests=0, cache_hits=0)
        while first.date() <= history_end:
            last = min(pd.Timestamp(history_end), pd.Timestamp(first.year, 12, 31))
            params = dict(common, models="ecmwf_ifs", hourly=",".join(("precipitation", "temperature_2m", *SOIL)),
                          start_date=str(first.date()), end_date=str((last + pd.Timedelta(days=1)).date()))
            print(f"历史模式 {first.date()}—{last.date()}", flush=True)
            payload, response_meta = fetch_hourly(root, HISTORY_API, params, session, stats)
            chunks.append(hourly_to_daily(payload, regions, first, last, model="HISTORY", run_at=None, fetched_at=response_meta["downloaded_at_utc"]))
            downloads += 1
            first = last + pd.Timedelta(days=1)
        for label, model in (("ECMWF", "ecmwf_ifs025"), ("GFS", "gfs_global")):
            params = dict(common, models=model, hourly="precipitation,temperature_2m", run=run.strftime("%Y-%m-%dT%H:%M"), forecast_days=16)
            print(f"{label} 共同起报 {run_at}", flush=True)
            payload, response_meta = fetch_hourly(root, FORECAST_API, params, session, stats)
            chunks.append(hourly_to_daily(payload, regions, today, forecast_end, model=label, run_at=run_at, fetched_at=response_meta["downloaded_at_utc"]))
            downloads += 1
        records = validate_records(pd.concat(chunks, ignore_index=True), config, history_end=history_end,
                                   forecast_start=today, forecast_end=forecast_end)
        # A source latency gap does not invalidate older history, but must not be called today's complete snapshot.
        tail = records[(records.model == "HISTORY") & (records.date == pd.Timestamp(history_end))]
        if tail.value.isna().any():
            raise ValueError("昨日历史模式资料尚不完整；保留上一成功快照")
        release_id = now.strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
        release = root / "releases" / release_id
        release.mkdir(parents=True, exist_ok=False)
        records.to_parquet(release / "records.parquet", index=False)
        meta = dict(schema_version="thailand-sugarcane-snapshot/1", release_id=release_id,
                    config_sha256=config["config_sha256"], records_sha256=sha256(release / "records.parquet"),
                    started_at_utc=fetched_at, published_at_utc=datetime.now(timezone.utc).isoformat(),
                    history_start="2020-01-01", history_end=str(history_end),
                    history_source_model="ecmwf_ifs", history_kind="historical_model_not_station_observations",
                    forecast_run_at=run_at, forecast_start=str(today), forecast_end=str(forecast_end),
                    timezone="Asia/Bangkok", point_count=len(regions),
                    null_history_values=int(records.loc[records.model.eq("HISTORY"), "value"].isna().sum()),
                    province_area_coverage=sum(r["national_share"] for r in regions),
                    source_urls=[HISTORY_API, FORECAST_API, config["source_url"]])
        atomic_json(release / "manifest.json", meta)
        if publish:
            atomic_json(root / "current.json", dict(release_id=release_id, manifest_sha256=sha256(release / "manifest.json")))
            load_snapshot(root, config)
        return dict(status="PUBLISHED" if publish else "CANDIDATE_READY", release_id=release_id,
                    requests=stats["requests"], cache_hits=stats["cache_hits"], source_reads=downloads,
                    publications=int(publish), manifest=meta)
