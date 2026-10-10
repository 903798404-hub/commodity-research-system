"""CNF-triggered CFETS FX and exact Sina contracts; latest available quotes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import json
import re
import time
from zoneinfo import ZoneInfo

import requests

from .model import START, PROFILES, expected_contracts, number
from .inputs import encoded

SCHEMA = "commodity-import-live-inputs/1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/"
FX_URLS = {"spot":BASE+"rfx-sp-quot.json", "swap":BASE+"rfx-sw-quot.json", "trade":BASE+"rfx-sp-iday-chrt.json"}


class LiveError(ValueError):
    pass


def source_time(stamp, captured):
    if stamp.utcoffset() is None or captured.utcoffset() is None or not timedelta(0) <= captured-stamp <= timedelta(days=7):
        raise LiveError("来源报价超过7天或时间无效")


def get_text(url, *, encoding="gb18030", max_bytes=256*1024):
    deadline = time.monotonic() + 20
    try:
        with requests.get(url, headers={"Referer":"https://finance.sina.com.cn/"}, timeout=(5,8), stream=True, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code != 200: raise LiveError("行情响应异常")
            raw = bytearray()
            for part in response.iter_content(8192):
                raw.extend(part)
                if len(raw) > max_bytes or time.monotonic() > deadline: raise LiveError("行情响应超时或超出上限")
        return raw.decode(encoding,errors="strict")
    except (requests.RequestException,UnicodeError):
        raise LiveError("行情连接或解析失败") from None


def parse_fx(bodies, now):
    if any(not isinstance(body,dict) or not isinstance(body.get("data"),dict)
           or (key != "trade" and (not isinstance(body.get("records"),list)
               or any(not isinstance(row,dict) for row in body["records"])))
           for key,body in bodies.items()):
        raise LiveError("CFETS响应结构异常")
    curve, source, errors = {}, dict(provider="CFETS",currency="USD/CNY",published_at=[]), {}
    spot = bodies.get("spot")
    if spot:
        rows = [r for r in spot["records"] if r.get("ccyPair") == "USD/CNY"]
        if len(rows) != 1: raise LiveError("CFETS在岸即期货币对异常")
        bid,ask = number(rows[0].get("bidPrc")),number(rows[0].get("askPrc"))
        if bid is not None and ask is not None and 0 < bid <= ask:
            stamp = datetime.fromisoformat(spot["data"]["showDateCN"]).replace(tzinfo=SHANGHAI)
            source_time(stamp,now)
            curve["0"] = (bid+ask)/2
            source.update(rate_kind="spot_bid_ask_mid",spot_bid=bid,spot_ask=ask,spot_source_url=FX_URLS["spot"])
            source["published_at"].append(stamp.isoformat())
    if "0" not in curve and bodies.get("trade"):
        trade = bodies["trade"]["data"]
        stamp = datetime.strptime(trade["lastDate"], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
        source_time(stamp,now)
        price = number(trade.get("spotPriceStr"))
        if price is not None and price > 0:
            curve["0"] = price
            source.update(rate_kind="latest_trade",spot_price=price,spot_source_url=FX_URLS["trade"])
            source["published_at"].append(stamp.isoformat())
    if "0" not in curve:
        errors["fx_spot"] = "CFETS在岸即期买卖报价和最新成交价均缺失"
        return curve,source,errors
    swaps = bodies.get("swap")
    if not swaps:
        errors["fx_forward"] = "CFETS在岸掉期点缺失，远期汇率留空"
        return curve,source,errors
    stamp = datetime.fromisoformat(swaps["data"]["showDateCN"]).replace(tzinfo=SHANGHAI)
    source_time(stamp,now)
    if stamp.date() != datetime.fromisoformat(source["published_at"][0]).date():
        errors["fx_forward"] = "即期与掉期来源日期不同，远期汇率留空"
        return curve,source,errors
    rows = [r for r in swaps["records"] if r.get("ccyPair") == "USD/CNY"]
    if len(rows) != 1: raise LiveError("CFETS在岸掉期货币对异常")
    points = {}
    for month,label in ((1,"1M"),(3,"3M"),(6,"6M"),(9,"9M"),(12,"1Y")):
        text = rows[0].get("label_"+label)
        if text in (None,"","---","--"):
            curve[str(month)] = None
            continue
        pair = str(text).split("/")
        if len(pair) != 2: raise LiveError("CFETS掉期点格式异常")
        low,high = map(number,pair)
        if low is None or high is None or low > high: raise LiveError("CFETS掉期点异常")
        curve[str(month)] = curve["0"] + (low+high)/2/10000
        points[str(month)] = text
    source.update(swap_points=points,points_divisor=10000,swap_source_url=FX_URLS["swap"])
    source["published_at"].append(stamp.isoformat())
    return curve,source,errors


def parse_domestic(text, symbols, now):
    prices,evidence,errors = dict.fromkeys(symbols),{},{}
    matches = re.findall(r'var hq_str_nf_((?:P|RM|OI)\d{4})="([^"\r\n]*)";',text)
    if len({m[0] for m in matches}) != len(matches) or any(s not in symbols for s,_ in matches):
        raise LiveError("国内行情合约集合异常")
    raw = dict(matches)
    for symbol in symbols:
        try:
            fields = raw.get(symbol,"").split(",")
            if len(fields) < 28: raise LiveError("具体合约未上市或无报价")
            if not fields[0].endswith(symbol[-4:]): raise LiveError("国内行情合约年份不符")
            stamp = datetime.combine(date.fromisoformat(fields[17]),datetime.strptime(fields[1].zfill(6),"%H%M%S").time(),SHANGHAI)
            source_time(stamp,now)
            price = number(fields[8])
            if price is None or price <= 0: raise LiveError("国内行情最新价缺失")
            prices[symbol] = price
            evidence[symbol] = dict(quoted_at=stamp.isoformat(),price_field="current_price",raw_contract=fields[0])
        except (ValueError,IndexError) as exc:
            errors[symbol] = str(exc) if isinstance(exc,LiveError) else "国内报价日期或格式无效"
    return prices,dict(provider="Sina",quotes=evidence),errors


def capture(commodity, *, now=None):
    fixed_time = now is not None
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    expected = expected_contracts(now.date(),commodity)
    symbols = sorted(expected)
    url = "https://hq.sinajs.cn/list=" + ",".join("nf_"+s for s in symbols)
    result = dict(schema_version=SCHEMA,business_date=now.date().isoformat(),commodity=commodity,captured_at=now.isoformat(),
        units=dict(cnf="USD/tonne",domestic="CNY/tonne",fx="CNY_per_USD"),parameters=dict(PROFILES[commodity]),
        contracts=expected,fx_currency="USD/CNY",fx_policy="soybean-spot-forward/1",fx_curve={},domestic=dict.fromkeys(expected),sources=dict(fx={},domestic={}),errors={})
    bodies = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {"domestic":pool.submit(get_text,url,max_bytes=32768),**{k:pool.submit(get_text,u,encoding="utf-8") for k,u in FX_URLS.items()}}
        for kind,future in futures.items():
            try:
                text = future.result()
                if kind == "domestic":
                    parsed_at = now if fixed_time else datetime.now(SHANGHAI)
                    result["domestic"],result["sources"][kind],errors = parse_domestic(text,symbols,parsed_at)
                    result["errors"].update(errors)
                else: bodies[kind] = json.loads(text)
            except (ValueError,TypeError,KeyError) as exc:
                result["errors"][kind] = str(exc) if isinstance(exc,LiveError) else "行情格式无效"
    try:
        parsed_at = now if fixed_time else datetime.now(SHANGHAI)
        result["fx_curve"],result["sources"]["fx"],errors = parse_fx(bodies,parsed_at)
        result["errors"].update(errors)
    except (ValueError,TypeError,KeyError) as exc:
        result["errors"]["fx"] = str(exc) if isinstance(exc,LiveError) else "在岸汇率格式无效"
    result["captured_at"] = now.isoformat() if fixed_time else datetime.now(SHANGHAI).isoformat()
    validate(result)
    return result


def validate(value):
    if not isinstance(value,dict): raise LiveError("录价行情结构无效")
    if any(not isinstance(value.get(k),dict) for k in ("fx_curve","parameters","contracts","domestic","sources","errors")):
        raise LiveError("录价行情字段结构无效")
    if value.get("schema_version") != SCHEMA or value.get("commodity") not in PROFILES: raise LiveError("录价行情版本或品种无效")
    day = date.fromisoformat(value["business_date"])
    stamp = datetime.fromisoformat(value["captured_at"])
    if day < START or stamp.utcoffset() is None or stamp.astimezone(SHANGHAI).date() != day: raise LiveError("录价行情日期无效")
    if value.get("fx_currency") != "USD/CNY" or value.get("units") != dict(cnf="USD/tonne",domestic="CNY/tonne",fx="CNY_per_USD"): raise LiveError("录价行情币种或单位无效")
    if value.get("fx_policy") != "soybean-spot-forward/1" or set(value.get("fx_curve",{})) - {"0","1","3","6","9","12"}: raise LiveError("汇率口径无效")
    if value.get("parameters") != dict(PROFILES[value["commodity"]]): raise LiveError("录价计算参数无效")
    expected = expected_contracts(day,value["commodity"])
    if value.get("contracts") != expected or set(value.get("domestic",{})) != set(expected): raise LiveError("录价国内合约集合无效")
    sources = value["sources"]
    if set(sources) != {"fx","domestic"} or not isinstance(value.get("errors"),dict): raise LiveError("行情来源缺失")
    if any(not isinstance(v,dict) for v in sources.values()): raise LiveError("行情来源结构无效")
    for symbol,price in value["domestic"].items():
        if price is None: continue
        evidence = sources["domestic"].get("quotes",{}).get(symbol,{})
        if number(price) is None or price <= 0 or evidence.get("price_field") != "current_price" or not evidence.get("raw_contract","").endswith(symbol[-4:]): raise LiveError("国内合约或价格凭证无效")
        source_time(datetime.fromisoformat(evidence["quoted_at"]),stamp)
    curve = value["fx_curve"]
    if any(v is not None and (number(v) is None or v <= 0) for v in curve.values()): raise LiveError("汇率数值无效")
    if any(v is not None for v in curve.values()):
        fx = sources["fx"]
        if fx.get("currency") != "USD/CNY" or fx.get("rate_kind") not in {"spot_bid_ask_mid","latest_trade"}: raise LiveError("在岸汇率凭证无效")
        stamps = fx.get("published_at",[])
        if not 1 <= len(stamps) <= 2: raise LiveError("汇率时间凭证缺失")
        for s in stamps: source_time(datetime.fromisoformat(s),stamp)
        if any(curve.get(str(k)) is not None for k in (1,3,6,9,12)) and len(stamps) != 2: raise LiveError("掉期时间凭证缺失")
        if fx["rate_kind"] == "spot_bid_ask_mid":
            bid,ask = number(fx.get("spot_bid")),number(fx.get("spot_ask"))
            if bid is None or ask is None or not 0 < bid <= ask: raise LiveError("即期买卖凭证无效")
            spot = (bid+ask)/2
            expected_url = FX_URLS["spot"]
        else:
            spot = number(fx.get("spot_price"))
            expected_url = FX_URLS["trade"]
        if spot is None or curve.get("0") != spot or fx.get("spot_source_url") != expected_url: raise LiveError("即期汇率与来源凭证不符")
        for tenor in (1,3,6,9,12):
            rate = curve.get(str(tenor))
            if rate is None: continue
            if fx.get("points_divisor") != 10000 or fx.get("swap_source_url") != FX_URLS["swap"] or datetime.fromisoformat(stamps[0]).date() != datetime.fromisoformat(stamps[1]).date(): raise LiveError("掉期来源凭证无效")
            pair = str(fx.get("swap_points",{}).get(str(tenor),"")).split("/")
            if len(pair) != 2: raise LiveError("掉期点凭证无效")
            bid,ask = map(number,pair)
            if bid is None or ask is None or bid > ask or rate != spot+(bid+ask)/2/10000: raise LiveError("远期汇率与掉期凭证不符")
    encoded(value)
    return day
