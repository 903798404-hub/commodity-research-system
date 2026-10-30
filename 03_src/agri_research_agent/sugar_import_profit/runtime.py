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


def historical_market(value, day):
    """Validate retrieved historical evidence without inventing a past capture time.

    This optional top-level payload leaves the original market snapshot untouched.
    Older readers ignore it and retain their original, possibly blank, calculation.
    """
    from datetime import date, datetime, time, timedelta
    from zoneinfo import ZoneInfo
    import math
    import re
    import xml.etree.ElementTree as ET
    from .model import TZ
    if (not isinstance(value, dict) or value.get('schema_version') != 'sugar-historical-close/1'
            or value.get('date') != day.isoformat()
            or value.get('fx_policy') != 'ECB_SPOT_ESTIMATE'
            or value.get('spot_estimate_authorized') is not True):
        raise ValueError('历史行情日期、版本或即期估算授权无效')
    retrieved = datetime.fromisoformat(value['retrieved_at'])
    if retrieved.utcoffset() is None or retrieved.astimezone(TZ).date() < day:
        raise ValueError('历史行情实际获取时间无效')
    # A Chinese quote-date snapshot can only use sessions completed by that day.
    asof = datetime.combine(day, time(23, 59, 59), TZ)
    local = asof.astimezone(ZoneInfo('America/New_York'))
    cutoff = local.date() - timedelta(days=local.time() < time(13, 30))
    def positive(v):
        if isinstance(v, bool): raise ValueError('历史价格无效')
        n = float(v)
        if not math.isfinite(n) or n <= 0: raise ValueError('历史价格无效')
        return n
    result = dict(ice={}, ice_closes={}, domestic={}, errors={}, captured_at=value['retrieved_at'],
                  fx_curve={}, fx_raw={}, fx_policy='即期汇率估算', fx_quote_date=day.isoformat())
    if not isinstance(value.get('ice'), dict) or not isinstance(value.get('domestic'), dict):
        raise ValueError('历史合约证据无效')
    for code, item in value['ice'].items():
        if not re.fullmatch(r'SB[HKNV]\d{2}', code): raise ValueError('历史原糖必须使用具体合约')
        body = item['raw']
        data = body['data']
        expected = f'SB{code[-2:]}{code[2]}'
        source = daily_close_url(code, datetime.combine(day + timedelta(days=1), time(8), TZ))
        if (item.get('source_url') != source or body.get('rc') != 0 or data.get('code') != expected
                or data.get('market') != 108 or data.get('decimal') != 2
                or not str(data.get('name', '')).startswith('糖11号')):
            raise ValueError('历史原糖合约、单位或来源不符')
        bars = data.get('klines')
        if not isinstance(bars, list) or not 1 <= len(bars) <= 15: raise ValueError('历史原糖日线无效')
        candidates, seen = [], set()
        for bar in bars:
            fields = bar.split(',')
            if len(fields) != 11: raise ValueError('历史原糖日线结构无效')
            d = date.fromisoformat(fields[0])
            opening, closing, high, low = map(positive, fields[1:5])
            volume = float(fields[5])
            if (d in seen or d.weekday() >= 5 or not math.isfinite(volume) or volume < 0
                    or not low <= min(opening, closing) <= max(opening, closing) <= high):
                raise ValueError('历史原糖日期、OHLC或成交量无效')
            seen.add(d)
            if d <= cutoff and volume > 0 and timedelta(0) <= cutoff - d <= timedelta(days=7):
                candidates.append((d, closing))
        if not candidates: raise ValueError('历史原糖缺少已完成且有成交的近期日线')
        d, price = max(candidates)
        result['ice'][code] = dict(price=None)
        result['ice_closes'][code] = dict(price=price, quote_date=d.isoformat(), quoted_at=d.isoformat(),
            time_kind='daily_close', provider='Eastmoney', contract=code, source_url=source)
    for code, item in value['domestic'].items():
        if not re.fullmatch(r'SR\d{2}(?:01|05|09)', code): raise ValueError('历史郑糖必须使用具体合约')
        source = 'https://stock2.finance.sina.com.cn/futures/api/json.php/InnerFuturesNewService.getDailyKLine?symbol=' + code
        raw = item['raw']
        if item.get('source_url') != source or raw.get('d') != day.isoformat():
            raise ValueError('历史郑糖日期或来源不符')
        opening, closing, high, low, volume = map(positive, (raw[k] for k in ('o', 'c', 'h', 'l', 'v')))
        if not low <= min(opening, closing) <= max(opening, closing) <= high:
            raise ValueError('历史郑糖OHLC无效')
        result['domestic'][code] = dict(price=closing, contract=code, provider='Sina',
            time_kind='daily_close', quote_date=day.isoformat(), quoted_at=day.isoformat(), source_url=source)
    fx = value['fx']
    if fx.get('source_url') != 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml':
        raise ValueError('历史汇率来源不符')
    xml = fx['raw_xml']
    if (not isinstance(xml, str) or len(xml.encode('utf-8')) > 131072
            or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper()):
        raise ValueError('历史汇率XML无效')
    ns = {'e': 'http://www.ecb.int/vocabulary/2002-08-01/eurofxref'}
    try:
        tree = ET.fromstring(xml)
    except ET.ParseError:
        raise ValueError('历史汇率XML无效') from None
    matches = [r for r in tree.findall('.//e:Cube[@time]', ns) if r.get('time') == day.isoformat()]
    if len(matches) != 1: raise ValueError('历史汇率没有唯一同日观察值')
    rates = {r.get('currency'): positive(r.get('rate')) for r in matches[0]}
    if len(rates) != len(matches[0]): raise ValueError('历史汇率币种重复')
    result['fx_spot'] = rates['CNY'] / rates['USD']
    result['fx_source'] = 'ECB同日参考汇率交叉计算（CNY/EUR ÷ USD/EUR）'
    return result


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
    return dict(price=price, contract=code, provider='Eastmoney', quoted_at=day.isoformat(), asof=now.isoformat(),
                quote_date=day.isoformat(), time_kind='daily_close', source_url=url, raw=body)


def validate_monthly_market(value, day):
    from datetime import datetime, timedelta
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
    closes = value.get('ice_closes', {})
    if not isinstance(closes, dict) or set(closes) - set(value['ice']):
        raise ValueError('原糖收盘价合约集合无效')
    for code, item in closes.items():
        if not isinstance(item, dict): raise ValueError('原糖收盘价结构无效')
        requested = datetime.fromisoformat(item.get('asof', ''))
        if requested.utcoffset() is None or not timedelta(0) <= captured - requested <= timedelta(minutes=1):
            raise ValueError('原糖收盘价取价时点无效')
        expected_item = eastmoney_daily_close(item.get('raw') or {}, code, requested)
        if any(item.get(k) != expected_item[k] for k in expected_item):
            raise ValueError('原糖收盘价或日期凭证不符')
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
    close_asof = datetime.now(TZ)
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {c: pool.submit(daily_close_text, daily_close_url(c, close_asof)) for c in missing_ice}
        for code, job in jobs.items():
            try:
                # Keep the original latest-price slot readable by retained images.
                result.setdefault('ice_closes', {})[code] = eastmoney_daily_close(json.loads(job.result()), code, close_asof)
                result['errors'].pop(code, None)
            except (ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError):
                result['errors'][code] = '原糖最新价及最近7天具体合约收盘价均不可用，留空'
    from .model import ice_quote
    if all(ice_quote(result, c).get('price') is not None for c in ice_codes):
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


DAILY_POLICY = 'sugar-domestic-close-1515/1'


def validate_daily_market(value):
    """An archive is a dated input, independent of whether any basis was entered."""
    from datetime import date, datetime, time
    from .model import TZ, today
    from agri_research_agent.commodity_import_margin.live import parse_fx
    day = date.fromisoformat(value['date'])
    if day.isoformat() > today(): raise ValueError('不能保存未来归档日')
    validate_monthly_market(value, day)
    captured = datetime.fromisoformat(value['captured_at']).astimezone(TZ)
    expected = datetime.combine(day, time(15, 15), TZ).isoformat()
    calendar = value.get('calendar') or {}
    if (value.get('archive_policy') != DAILY_POLICY or value.get('scheduled_at') != expected
            or not time(15, 15) <= captured.time() <= time(15, 30)
            or calendar.get('provider') != 'AkShare/Sina'
            or calendar.get('trading_day') != day.isoformat()
            or date.fromisoformat(calendar['covered_through']) < day):
        raise ValueError('15:15行情归档时段或交易日历不符')
    if day.weekday() >= 5: raise ValueError('周末不能作为国内收盘归档日')
    for row in value['domestic'].values():
        if row.get('price') is None: continue
        stamp = datetime.fromisoformat(row['quoted_at']).astimezone(TZ)
        if stamp.date() != day or not time(15) <= stamp.time() <= time(15, 10) or row.get('time_kind') != 'daily_close':
            raise ValueError('郑糖归档必须为同日15点收盘报价')
    if value['fx_curve']:
        _, source, _ = parse_fx(value['fx_raw'], captured)
        if any(datetime.fromisoformat(t).astimezone(TZ).date() != day for t in source['published_at']):
            raise ValueError('归档汇率必须与报价日相同')
    if not (value.get('ice_closes') or value['fx_curve'] or any(r.get('price') is not None for kind in ('ice', 'domestic') for r in value[kind].values())):
        raise ValueError('本次未取得有效行情，不生成空白归档')
    return value


def capture_daily_market(*, now=None, calendar=None, capture=None, extra_quotes=()):
    """Only the scheduled current-day window may collect; no historical clock override in CLI."""
    from datetime import date, datetime, time
    from .model import TZ, month_periods, default_mapping
    now = now or datetime.now(TZ)
    if now.utcoffset() is None: raise ValueError('归档时钟必须包含时区')
    now = now.astimezone(TZ)
    day = now.date()
    if not time(15, 15) <= now.time() <= time(15, 30):
        raise ValueError('已错过当日15:15归档窗口，不以当前行情补造历史')
    if day.weekday() >= 5:
        return None
    if calendar is None:
        from agri_research_agent.soybean_margin.api_sources import _worker
        calendar = _worker('calendar', 35)
    dates = {date.fromisoformat(d) for d in calendar}
    if not dates or day > max(dates): raise ValueError('交易日历覆盖不足，停止归档')
    if day not in dates: return None
    quotes = [dict(ice_contract=default_mapping(p)[0], sr_contract=default_mapping(p)[1]) for p in month_periods(day)]
    quotes.extend(extra_quotes)
    value = (capture or capture_monthly_market)(day, quotes)
    value.update(archive_policy=DAILY_POLICY, scheduled_at=datetime.combine(day, time(15, 15), TZ).isoformat(),
                 calendar=dict(provider='AkShare/Sina', trading_day=day.isoformat(), covered_through=max(dates).isoformat()))
    # The broad interactive reader permits delayed quotes; a dated archive does not.
    for code, row in value['domestic'].items():
        if row.get('price') is None: continue
        stamp = datetime.fromisoformat(row['quoted_at']).astimezone(TZ)
        if stamp.date() != day or not time(15) <= stamp.time() <= time(15, 10):
            value['domestic'][code] = dict(price=None)
            value['errors'][code] = '未取得同日15点收盘价，留空'
        else:
            row['time_kind'] = 'daily_close'
    if value['fx_curve']:
        from agri_research_agent.commodity_import_margin.live import parse_fx
        _, source, _ = parse_fx(value['fx_raw'], datetime.fromisoformat(value['captured_at']))
        if any(datetime.fromisoformat(t).astimezone(TZ).date() != day for t in source['published_at']):
            value.update(fx_curve={}, fx_raw={})
            value['errors']['fx_archive'] = '未取得同日汇率，留空'
    validate_daily_market(value)
    return value


def archive_main(argv=None):
    """Small server entry point used by the independent 15:15 systemd timer."""
    import argparse
    import json
    from datetime import datetime
    from .model import TZ
    from .store import Store
    parser = argparse.ArgumentParser(description='白糖每日15:15行情归档，后补报价按所属日读取')
    parser.add_argument('--check', action='store_true', help='只检查运行身份和已存行情，不采集、不写入')
    args = parser.parse_args(argv)
    try:
        path, _, authorize, enabled = storage_context()
        if not enabled or not monthly_capture_enabled(): raise ValueError('白糖保存或采集尚未启用')
        store = Store(path, authorize)
        day = datetime.now(TZ).date()
        existing = store.daily_market(day)
        if args.check or existing:
            print(json.dumps(dict(status='CHECK_ONLY' if args.check else 'ALREADY_ARCHIVED', date=day.isoformat(),
                                  version=existing['version'] if existing else None), ensure_ascii=False))
            return 0
        extra = [q for r in store.monthly_records() if r['payload']['date'] == day.isoformat() for q in r['payload']['quotes']]
        value = capture_daily_market(extra_quotes=extra)
        if value is None:
            print(json.dumps(dict(status='NON_TRADING_DAY', date=day.isoformat())))
            return 0
        version = store.save_daily_market(value, expected_version=None)
        print(json.dumps(dict(status='ARCHIVED_WITH_MISSING' if value['errors'] else 'ARCHIVED', date=day.isoformat(),
            version=version, captured_at=value['captured_at'], errors=value['errors']), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps(dict(status='FAILED', error=str(exc)), ensure_ascii=False))
        return 2
