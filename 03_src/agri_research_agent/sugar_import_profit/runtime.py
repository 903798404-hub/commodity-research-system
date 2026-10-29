"""Reuse the protected workbench manual-CNF mount; never fall back in deployment."""
from __future__ import annotations
import os
from pathlib import Path
from agri_research_agent.shared.runtime_context import (
    RuntimeAuthorizationError, RuntimeClassification, assert_runtime_write,
    establish_application_service_context, load_runtime_identity,
)

SOURCE = Path(__file__).resolve().parents[3]
ROOT = Path("/runtime")
STORAGE = Path("import-profit/operational/cnf/sugar-import/sugar-import.sqlite3")


def deployed():
    return bool(os.getenv("MARKET_DATA_GIT_HEAD") or os.getenv("MARKET_DATA_EXECUTION_GRANT")
                or (SOURCE / "RELEASE.json").exists())


def storage_context():
    if deployed():
        if os.getenv("SUGAR_IMPORT_LOCAL_PREVIEW") == "1":
            raise ValueError("正式环境不能使用本机白糖预览身份")
        try:
            identity = load_runtime_identity(ROOT)
        except (OSError, ValueError, RuntimeAuthorizationError):
            raise ValueError("白糖利润看板缺少有效的工作台运行身份") from None
        if (identity.classification not in {RuntimeClassification.FORMAL, RuntimeClassification.CANDIDATE_VALIDATION}
                or identity.module_id != "shared-intraday"):
            raise ValueError("白糖利润看板运行身份不符")
        path = ROOT / STORAGE
        if not (ROOT / STORAGE.parent.parent).is_dir() or path.resolve() != path.absolute():
            raise ValueError("白糖独立业务目录无效")
        return path, ROOT / "01_data/processed/sugar_import_history", authorize_write, os.getenv("SUGAR_IMPORT_ALLOW_SAVE") == "1"
    if os.getenv("SUGAR_IMPORT_ALLOW_SAVE") == "1":
        raise ValueError("白糖服务器保存缺少正式身份")
    raw = os.getenv("SUGAR_IMPORT_LOCAL_ROOT")
    local = Path(raw).absolute() if raw else SOURCE / "01_data/candidates/sugar_import_profit/local-preview"
    if local.resolve() != local or not local.is_relative_to(SOURCE / "01_data/candidates/sugar_import_profit"):
        raise ValueError("本机白糖预览必须使用独立候选目录")
    return local / "sugar-import.sqlite3", local / "history", authorize_write, os.getenv("SUGAR_IMPORT_LOCAL_PREVIEW") == "1"


def authorize_write(path):
    expected, _, _, enabled = storage_context()
    if not enabled or Path(path).absolute() != expected or Path(path).resolve() != expected:
        raise ValueError("白糖利润保存权限或路径无效")
    if deployed():
        try:
            context = establish_application_service_context(service_id="spread-dashboard", module_id="shared-intraday", runtime_root=ROOT)
            assert_runtime_write(context, path)
        except (OSError, ValueError, RuntimeAuthorizationError):
            raise ValueError("白糖利润保存需要有效的工作台服务写入权限") from None


def monthly_capture_enabled():
    # Source requests are read-only. Preserve existing server's explicit enable boundary.
    return not deployed() or os.getenv('SUGAR_IMPORT_ALLOW_CAPTURE') == '1'


def yahoo_text(url):
    """Bounded read of the exact ICE contract; Yahoo requires a browser user agent."""
    import time
    import requests
    deadline = time.monotonic() + 20
    try:
        with requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=(5, 8),
                          stream=True, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code != 200: raise ValueError('备用行情响应异常')
            raw = bytearray()
            for part in response.iter_content(8192):
                raw.extend(part)
                if len(raw) > 131072 or time.monotonic() > deadline:
                    raise ValueError('备用行情超时或超出上限')
        return raw.decode('utf-8', errors='strict')
    except (requests.RequestException, UnicodeError):
        raise ValueError('备用行情连接或解析失败') from None


def yahoo_ice_quote(meta, code, now):
    from datetime import datetime
    import math
    from .model import TZ
    from agri_research_agent.commodity_import_margin.live import source_time
    if (meta.get('symbol') != code + '.NYB' or meta.get('currency') != 'USX'
            or meta.get('exchangeName') != 'NYB' or meta.get('instrumentType') != 'FUTURE'):
        raise ValueError('备用原糖合约、交易所或美分单位不符')
    price = meta.get('regularMarketPrice')
    if isinstance(price, bool) or not isinstance(price, (float, int)) or not math.isfinite(price) or price <= 0:
        raise ValueError('备用原糖最新报价无效')
    stamp = datetime.fromtimestamp(meta['regularMarketTime'], TZ)
    source_time(stamp, now)
    return dict(price=price, contract=code, provider='Yahoo Finance', quoted_at=stamp.isoformat(),
        time_kind='exchange_quote', source_url='https://query1.finance.yahoo.com/v8/finance/chart/' + code + '.NYB?interval=1d&range=5d', raw=meta)


def daily_close_url(code, now):
    """Request completed daily bars for this exact contract, never a continuous series."""
    import re
    from datetime import timedelta, time
    from zoneinfo import ZoneInfo
    if not re.fullmatch(r'SB[HKNV]\d{2}', code) or now.utcoffset() is None:
        raise ValueError('收盘价合约或时间无效')
    local = now.astimezone(ZoneInfo('America/New_York'))
    # https://www.ice.com/products/23/Sugar-No-11-Futures (Trading Hours)
    # ICE SB normally closes at 13:00 New York time. Allow publication latency;
    # an early-close holiday is conservatively excluded until this same cutoff.
    cutoff = local.date() - timedelta(days=local.time() < time(13, 30))
    symbol = f'SB{code[-2:]}{code[2]}'
    return ('https://push2his.eastmoney.com/api/qt/stock/kline/get'
            f'?secid=108.{symbol}&klt=101&fqt=0&lmt=15&end={cutoff:%Y%m%d}'
            '&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61')


def daily_close_text(url):
    from agri_research_agent.commodity_import_margin.live import get_text
    return get_text(url, encoding='utf-8', max_bytes=32768)


def eastmoney_daily_close(body, code, now):
    """Validate contract identity, completed-session date, OHLC and traded volume."""
    from datetime import date, datetime, time, timedelta
    from zoneinfo import ZoneInfo
    from agri_research_agent.soybean_margin.model import number
    url = daily_close_url(code, now)
    symbol = f'SB{code[-2:]}{code[2]}'
    if not isinstance(body, dict) or not isinstance(body.get('data'), dict):
        raise ValueError('原糖收盘价响应结构无效')
    data = body.get('data') or {}
    if (body.get('rc') != 0 or data.get('code') != symbol or data.get('market') != 108
            or data.get('decimal') != 2 or not str(data.get('name', '')).startswith('糖11号')):
        raise ValueError('原糖收盘价合约、市场或单位不符')
    bars = data.get('klines')
    if not isinstance(bars, list) or not 1 <= len(bars) <= 15:
        raise ValueError('原糖收盘价日线缺失或超限')
    ny = ZoneInfo('America/New_York')
    local = now.astimezone(ny)
    cutoff = local.date() - timedelta(days=local.time() < time(13, 30))
    candidates, dates = [], set()
    for text in bars:
        if not isinstance(text, str): raise ValueError('原糖收盘价日线结构无效')
        fields = text.split(',')
        if len(fields) != 11: raise ValueError('原糖收盘价日线结构无效')
        day = date.fromisoformat(fields[0])
        if day in dates or day > cutoff or day.weekday() >= 5: raise ValueError('原糖收盘价日期重复或尚未收盘')
        dates.add(day)
        opening, closing, high, low, volume = map(number, fields[1:6])
        if (any(v is None or v <= 0 for v in (opening, closing, high, low))
                or not low <= min(opening, closing) <= max(opening, closing) <= high
                or volume is None or volume < 0):
            raise ValueError('原糖收盘价日线价格或成交量无效')
        if volume > 0 and timedelta(0) <= local - datetime.combine(day, time(13), ny) <= timedelta(days=7):
            candidates.append((day, closing))
    if not candidates: raise ValueError('具体合约最近7天没有有效已收盘成交')
    day, price = max(candidates)
    return dict(price=price, contract=code, provider='Eastmoney', quoted_at=day.isoformat(),
                quote_date=day.isoformat(), time_kind='daily_close', source_url=url, raw=body)


def validate_monthly_market(value, day):
    from datetime import datetime
    import math
    import re
    from .model import TZ
    from agri_research_agent.commodity_import_margin.live import source_time
    if value.get('schema_version') != 'sugar-monthly-market/1' or value.get('date') != day.isoformat():
        raise ValueError('行情日期或版本不符')
    captured = datetime.fromisoformat(value['captured_at'])
    if captured.utcoffset() is None or captured.astimezone(TZ).date() != day:
        raise ValueError('行情获取日期不符')
    if value.get('units') != dict(ice='US_cents/lb', domestic='CNY/tonne', fx='CNY_per_USD'):
        raise ValueError('行情单位不符')
    for kind, pattern, provider in (('ice', r'SB[HKNV]\d{2}', 'Eastmoney'),
                                    ('domestic', r'SR\d{4}', 'Sina')):
        for code, item in value[kind].items():
            if not re.fullmatch(pattern, code): raise ValueError('行情合约无效')
            price = item.get('price')
            if price is None: continue
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
                raise ValueError('行情价格无效')
            if kind == 'ice' and item.get('time_kind') == 'daily_close':
                expected_item = eastmoney_daily_close(item.get('raw') or {}, code, captured)
                if any(item.get(k) != expected_item[k] for k in expected_item):
                    raise ValueError('原糖收盘价或日期凭证不符')
                continue
            if kind == 'ice' and item.get('provider') == 'Yahoo Finance':
                expected_item = yahoo_ice_quote(item.get('raw') or {}, code, captured)
                if any(item.get(k) != expected_item[k] for k in ('price', 'quoted_at', 'contract', 'source_url', 'time_kind')):
                    raise ValueError('备用原糖价格或时间凭证不符')
                continue
            if item.get('provider') != provider or item.get('contract') != code or not item.get('raw'):
                raise ValueError('行情合约来源凭证缺失')
            if kind == 'ice':
                raw = item['raw']
                expected = f'SB{code[-2:]}{code[2]}'
                if raw.get('dm') != expected or raw.get('uid') != 'NYBOT|' + expected or raw.get('p') != price:
                    raise ValueError('原糖完整合约或价格凭证不符')
                actual_stamp = datetime.fromtimestamp(raw['utime'], TZ).isoformat()
                if item.get('quoted_at') != actual_stamp: raise ValueError('原糖来源更新时间不符')
            else:
                raw = item['raw']
                if not raw[0].endswith(code[-4:]) or float(raw[8]) != price:
                    raise ValueError('郑糖合约或价格凭证不符')
                actual_stamp = datetime.strptime(raw[17] + ' ' + raw[1].zfill(6), '%Y-%m-%d %H%M%S').replace(tzinfo=TZ).isoformat()
                if item.get('quoted_at') != actual_stamp: raise ValueError('郑糖来源时间不符')
            source_time(datetime.fromisoformat(item['quoted_at']), captured)
    # Reuse palm's validation of CFETS raw spot/swap evidence and tenor policy.
    from agri_research_agent.commodity_import_margin.live import parse_fx
    curve, _, _ = parse_fx(value.get('fx_raw', {}), captured)
    if curve != value.get('fx_curve'): raise ValueError('汇率与来源凭证不符')


def capture_monthly_market(day, quotes):
    """CNF-triggered, bounded exact-contract fetch; failures clear that source."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime
    import json
    from .model import TZ, today
    from agri_research_agent.commodity_import_margin.live import get_text, parse_fx, source_time
    from agri_research_agent.commodity_import_margin.live import FX_URLS
    from agri_research_agent.soybean_margin.model import number
    if day.isoformat() != today(): raise ValueError('历史日期只使用当日保存的行情')
    if not monthly_capture_enabled(): raise ValueError('服务器白糖录价取价入口未启用')
    import re
    if any(not re.fullmatch(r'SB[HKNV]\d{2}', r.get('ice_contract', ''))
           or not re.fullmatch(r'SR\d{2}(?:01|05|09)', r.get('sr_contract', '')) for r in quotes):
        raise ValueError('取价必须使用完整具体合约')
    ice_codes = sorted({r['ice_contract'] for r in quotes})
    sr_codes = sorted({r['sr_contract'] for r in quotes})
    result = dict(schema_version='sugar-monthly-market/1', date=day.isoformat(),
        units=dict(ice='US_cents/lb', domestic='CNY/tonne', fx='CNY_per_USD'),
        ice={c: dict(price=None) for c in ice_codes}, domestic={c: dict(price=None) for c in sr_codes},
        fx_curve={}, fx_raw={}, errors={})
    em_url = ('https://futsseapi.eastmoney.com/list/NYBOT?pageIndex=0&pageSize=1000&orderBy=dm&sort=desc'
              '&token=58b2fa8f54638b60b87d69b31969089c')
    domestic_url = 'https://hq.sinajs.cn/list=' + ','.join('nf_' + c for c in sr_codes)
    urls = dict(ice=em_url, domestic=domestic_url, **FX_URLS)
    bodies = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {k: pool.submit(get_text, url, encoding='gb18030' if k == 'domestic' else 'utf-8') for k, url in urls.items()}
        for kind, job in jobs.items():
            try: bodies[kind] = job.result()
            except (ValueError, TypeError): result['errors'][kind] = '来源连接失败，价格留空'
    now = datetime.now(TZ)
    try:
        body = json.loads(bodies.get('ice', '{}'))
        records = body['list']
        if len(records) != body['total'] or len({r['dm'] for r in records}) != len(records):
            raise ValueError('原糖合约列表不完整')
        indexed = {r['dm']: r for r in records}
        for code in ice_codes:
            raw = indexed.get(f'SB{code[-2:]}{code[2]}')
            price = number(raw.get('p')) if raw else None
            try:
                if price is None or price <= 0: raise ValueError('最新价缺失，不使用昨结或连续合约替代')
                stamp = datetime.fromtimestamp(raw['utime'], TZ)
                source_time(stamp, now)
                result['ice'][code] = dict(price=price, contract=code, provider='Eastmoney',
                    quoted_at=stamp.isoformat(), time_kind='provider_update', source_url=em_url, raw=raw)
            except (ValueError, TypeError, KeyError, OverflowError): result['errors'][code] = '原糖最新价或有效来源时间缺失'
    except (ValueError, TypeError, KeyError): result['errors']['ice'] = '原糖列表不可用，价格留空'
    missing_ice = [c for c in ice_codes if result['ice'][c]['price'] is None]
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {c: pool.submit(yahoo_text, 'https://query1.finance.yahoo.com/v8/finance/chart/' + c + '.NYB?interval=1d&range=5d') for c in missing_ice}
        for code, job in jobs.items():
            try:
                meta = json.loads(job.result())['chart']['result'][0]['meta']
                result['ice'][code] = yahoo_ice_quote(meta, code, datetime.now(TZ))
                result['errors'].pop(code, None)
            except (ValueError, TypeError, KeyError, IndexError, OverflowError):
                result['errors'][code] = '原糖具体合约两来源均无有效报价，留空'
    missing_ice = [c for c in ice_codes if result['ice'][c]['price'] is None]
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {c: pool.submit(daily_close_text, daily_close_url(c, datetime.now(TZ))) for c in missing_ice}
        for code, job in jobs.items():
            try:
                result['ice'][code] = eastmoney_daily_close(json.loads(job.result()), code, datetime.now(TZ))
                result['errors'].pop(code, None)
            except (ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError):
                result['errors'][code] = '原糖最新价及最近7天具体合约收盘价均不可用，留空'
    if all(result['ice'][c]['price'] is not None for c in ice_codes):
        result['errors'].pop('ice', None)
    try:
        import re
        text = bodies.get('domestic', '')
        matches = re.findall(r'var hq_str_nf_(SR\d{4})="([^"\r\n]*)";', text)
        if len(dict(matches)) != len(matches) or any(c not in sr_codes for c, _ in matches): raise ValueError('郑糖合约集合异常')
        for code, fields in matches:
            try:
                raw = fields.split(',')
                if len(raw) < 28 or not raw[0].endswith(code[-4:]): raise ValueError('合约年份不符')
                stamp = datetime.strptime(raw[17] + ' ' + raw[1].zfill(6), '%Y-%m-%d %H%M%S').replace(tzinfo=TZ)
                source_time(stamp, now)
                price = number(raw[8])
                if price is None or price <= 0: raise ValueError('最新价缺失')
                result['domestic'][code] = dict(price=price, contract=code, provider='Sina',
                    quoted_at=stamp.isoformat(), source_url=domestic_url, raw=raw)
            except (ValueError, KeyError, IndexError): result['errors'][code] = '郑糖最新价或有效来源时间缺失'
        for code in sr_codes:
            if result['domestic'][code]['price'] is None: result['errors'][code] = '郑糖具体合约尚无有效报价'
    except (ValueError, TypeError): result['errors']['domestic'] = '郑糖响应无效，价格留空'
    fx_raw = {}
    for kind in FX_URLS:
        try:
            if kind in bodies: fx_raw[kind] = json.loads(bodies[kind])
        except ValueError: result['errors'][kind] = '汇率响应无效'
    try:
        curve, _, errors = parse_fx(fx_raw, now)
        result.update(fx_curve=curve, fx_raw=fx_raw)
        result['errors'].update(errors)
    except (ValueError, KeyError, TypeError): result['errors']['fx'] = 'CFETS汇率不可用，留空'
    result['captured_at'] = datetime.now(TZ).isoformat()
    if datetime.now(TZ).date() != day: raise ValueError('获取行情跨越业务日期，请重新获取')
    validate_monthly_market(result, day)
    return result
