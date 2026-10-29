"""Brazil/Thailand sugar import-profit workbench; manual basis/freight and explicit exact-contract market capture."""
from __future__ import annotations
from datetime import date
import json
import sqlite3
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from computation_cache import cache_computation
from agri_research_agent.sugar_import_profit.model import COUNTRIES, LABELS, today, ice_quote
from agri_research_agent.sugar_import_profit.runtime import storage_context
from agri_research_agent.sugar_import_profit.store import Store


@st.cache_data(show_spinner=False, max_entries=4)
def _cached_freight(root, identity):
    from agri_research_agent.sugar_import_profit.freight import load_freight
    return load_freight(root)


def _public_freight(root):
    """Read the sealed source once per artifact change, with no page-time downloads."""
    pointer = root / 'current.json'
    if not pointer.exists(): return []
    from pathlib import Path
    if pointer.stat().st_size > 16384: raise ValueError('泛糖运费索引无效')
    manifest = json.loads(pointer.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict): raise ValueError('泛糖运费索引无效')
    name = manifest.get('file')
    if not isinstance(name, str) or Path(name).name != name: raise ValueError('泛糖运费文件名无效')
    target = root / name
    stat = target.stat()
    return _cached_freight(root, (pointer.read_bytes(), stat.st_mtime_ns, stat.st_size))


def _freight_source(row):
    origin = row.get('freight_origin') or {}
    kind = origin.get('kind', 'missing' if row.get('freight') is None else 'manual')
    source = (origin.get('sources') or [{}])[0]
    return dict(运费来源={'missing':'缺失', 'manual':'人工', 'hisugar_reference':'泛糖公开参考'}[kind],
        运费原始日期=origin.get('quote_date', ''), 运费来源链接=source.get('article_url', ''),
        运费原图校验值=source.get('image_sha256', ''))


def monthly_table(rows):
    return pd.DataFrame([dict(船期=r['period'], 原糖合约=r['ice_contract'],
        原糖价格=r['ice'], 原糖口径=r.get('ice_price_kind', ''), 原糖价格日期=r.get('ice_quote_date', ''),
        升贴水=r['quote'], 海运费=r['freight'], 汇率=r['fx'],
        郑糖合约=r['sr_contract'], 郑糖价格=r['sr'], 配额内成本=r['cost_in'],
        配额外成本=r['cost_out'], 配额内进口利润=r['margin_in'], 配额外进口利润=r['margin_out'])
        for r in rows]).rename(columns={'原糖价格':'原糖（美分/磅）', '升贴水':'升贴水（美分/磅）',
        '海运费':'海运费（美元/吨）', '郑糖价格':'郑糖（元/吨）'})


def monthly_html(rows, country='巴西', day=''):
    from html import escape
    from pathlib import Path
    from soybean_margin_page import _cell
    css = (Path(__file__).parent / 'soybean_margin_assets/tables.css').read_text(encoding='utf-8')
    frame = monthly_table(rows)
    result = f'<style>{css}</style><section class="panel"><header>中国进口白糖盘面利润 · {escape(country)}<small>{escape(str(day))} · 元/吨</small></header><div class="scroll"><table class="terminal-table"><thead><tr>'
    result += ''.join('<th>' + escape(c) + '</th>' for c in frame.columns) + '</tr></thead><tbody>'
    for row in frame.to_dict('records'):
        cells = []
        for key, value in row.items():
            if isinstance(value, str): text = escape(value)
            elif key == '汇率': text = f'{value:.4f}' if value is not None and pd.notna(value) else '—'
            else: text = _cell(value, profit='利润' in key)
            cells.append('<td>' + text + '</td>')
        result += '<tr>' + ''.join(cells) + '</tr>'
    return result + '</tbody></table></div><footer>原糖与升贴水：美分/磅；海运费：美元/吨；成本、利润：元/吨。进口利润 = 对应郑糖合约价格 − 进口加工后成本。<br>原糖最新价不可用时采用同一合约最近7天已收盘日线，实际交易日见“原糖价格日期”；收盘价不代表实时价格。<br>保险、利息、手续费、税率与加工费见下方固定参数；缺少报价或具体合约行情时留空。</footer></section>'


def _freight_grid(rows):
    observed = pd.DataFrame([dict(date=r['date'], freight_usd_per_tonne=r['freight_usd_per_tonne'],
        published_at=r['sources'][0]['published_at'], article_url=r['sources'][0]['article_url'],
        image_url=r['sources'][0]['image_url'], image_sha256=r['sources'][0]['image_sha256']) for r in rows])
    return pd.DataFrame({'date': pd.date_range(rows[0]['date'], today()).strftime('%Y-%m-%d')}).merge(observed, on='date', how='left')


def _freight_history(rows):
    """Original daily reference observations, separate from calculated profit history."""
    st.subheader('泛糖公开运费历史 · 巴西')
    st.caption('泛糖测算采用运费，单位美元/吨；不是SYCAC00或逐月船期成交报价。没有原始数值的日期留空，不用利润反推运费。')
    if not rows:
        st.info('尚无已交付的公开运费历史；可继续手工录入。')
        return
    grid = _freight_grid(rows)
    from agri_research_agent.shared.chart_style import CURRENT_YEAR_COLOR, GRID_COLOR
    chart = go.Figure(go.Scatter(x=grid.date, y=grid.freight_usd_per_tonne, mode='lines+markers',
        connectgaps=False, line=dict(color=CURRENT_YEAR_COLOR, width=2)))
    chart.update_layout(height=300, yaxis_title='美元/吨', plot_bgcolor='white',
        xaxis=dict(gridcolor=GRID_COLOR), yaxis=dict(gridcolor=GRID_COLOR), margin=dict(l=30,r=10,t=10,b=30))
    st.plotly_chart(chart, width='stretch', key='sugar-public-freight-history')
    st.caption(f"已取得{len(rows)}个原始日期，范围{rows[0]['date']}—{rows[-1]['date']}；日历中的空白不表示零运费。")
    st.dataframe(grid.sort_values('date', ascending=False), hide_index=True, width='stretch')
    st.download_button('导出泛糖公开运费历史（含空白日期及来源）', _csv(grid), 'hisugar-freight-history.csv', 'text/csv', key='sugar-public-freight-export')


def _monthly(store, enabled, freight_root=None):
    import hashlib
    from agri_research_agent.sugar_import_profit.model import (
        RESEARCH_VERSION, RESEARCH_PARAMETERS, QUOTE_MODES, month_periods, default_mapping,
        monthly_rows, validate_monthly)
    from agri_research_agent.sugar_import_profit.runtime import capture_monthly_market
    from agri_research_agent.sugar_import_profit.freight import reference_on, provenance
    try:
        public_freight = _public_freight(freight_root) if freight_root else []
    except (OSError, ValueError, KeyError, TypeError) as exc:
        st.warning('公开运费暂不可用，手工测算仍可使用：' + str(exc))
        public_freight = []
    st.caption('未来12个月船期 · 当月表示次年同月 · 原糖和郑糖按具体合约取价')
    controls = st.columns([1, 2])
    country = controls[0].radio('产地', COUNTRIES, horizontal=True, key='sugar-month-country')
    day = controls[1].date_input('报价日期', date.fromisoformat(today()), max_value=date.fromisoformat(today()), key='sugar-month-day')
    reference = reference_on(public_freight, day.isoformat(), country)
    if reference:
        st.caption(f"{day} 泛糖公开参考运费：{reference['freight_usd_per_tonne']:g}美元/吨。新录价默认将其作为各船期统一测算假设，未视作远期实际报价；手工值优先。")
        st.markdown('[查看原始来源](' + reference['sources'][0]['article_url'] + ')')
    elif country == '巴西':
        st.caption('该报价日期没有泛糖公开运费，默认留空；不借用邻日数据。仍可在录价区手工填写。')
        if public_freight:
            latest = public_freight[-1]
            st.caption(f"仅供核对：最近原始记录{latest['date']}为{latest['freight_usd_per_tonne']:g}美元/吨，不自动带入本日。")
    with st.expander('查看泛糖公开运费历史与来源'):
        _freight_history(public_freight)
    key = f'sugar-month:{country}:{day}'
    records = store.monthly_records()
    current = next((r for r in records if r['payload']['date'] == day.isoformat() and r['payload']['country'] == country and r['payload']['schema_version'] == RESEARCH_VERSION), None)
    if st.button('重新载入月份记录', key=key + ':reload'):
        for name in list(st.session_state):
            if name.startswith(key + ':') and name != key + ':reload': del st.session_state[name]
        st.rerun()
    if key + ':initial' not in st.session_state:
        payload = current['payload'] if current and current['payload']['schema_version'] == RESEARCH_VERSION else dict(schema_version=RESEARCH_VERSION, date=day.isoformat(), country=country,
            quote_mode=QUOTE_MODES[0], source='人工录入升贴水及海运费', parameters=dict(next((r['payload']['parameters'] for r in records if r['payload']['country'] == country and r['payload']['schema_version'] == RESEARCH_VERSION and r['payload']['date'] < day.isoformat()), RESEARCH_PARAMETERS)), market=None,
            quotes=[dict(period=p, ice_contract=default_mapping(p)[0], sr_contract=default_mapping(p)[1],
                         quote=None, freight=reference['freight_usd_per_tonne'] if reference else None,
                         freight_origin=provenance(reference, reference['freight_usd_per_tonne'] if reference else None),
                         insurance=None, rizhao=None, rizhao_date='') for p in month_periods(day)])
        st.session_state[key + ':initial'] = payload
        st.session_state[key + ':version'] = current['version'] if current else None
        st.session_state[key + ':market'] = payload['market']
    initial = st.session_state[key + ':initial']
    fields = dict(period='船期', quote='升贴水（美分/磅）', freight='海运费（美元/吨）', ice_contract='ICE合约', sr_contract='郑糖合约')
    mode = QUOTE_MODES[0]
    with st.expander('录入升贴水和海运费'):
        st.caption('只填变化的升贴水和海运费，平水填0，无报价留空。合约可按实际报价调整。')
        st.caption('巴西按船期加2个月匹配首个郑糖1/5/9月：1月→5月；ICE取船期后首个3/5/7/10月合约：1月→3月。此为研究映射，可修改。')
        edited = st.data_editor(pd.DataFrame(initial['quotes'])[['period', 'quote', 'freight', 'ice_contract', 'sr_contract']].sort_values('period').rename(columns=fields),
            hide_index=True, disabled=['船期'], num_rows='fixed', height=460, key=key + ':editor',
            column_config={name: st.column_config.NumberColumn(name, min_value=0. if field == 'freight' else None, format='%.4f')
                           for field, name in fields.items() if field in ('quote', 'freight')})
    quotes = edited.rename(columns={v: k for k, v in fields.items()}).astype(object).where(pd.notna(edited.rename(columns={v: k for k, v in fields.items()})), None).to_dict('records')
    initial_quotes = {r['period']: r for r in initial['quotes']}
    for r in quotes:
        old = initial_quotes[r['period']]
        if r['freight'] is None:
            r['freight_origin'] = provenance(None, None)
        elif r['freight'] == old.get('freight'):
            r['freight_origin'] = old.get('freight_origin') or provenance(None, r['freight'])
        else:
            r['freight_origin'] = provenance(None, r['freight'])
        r.update(insurance=None, rizhao=None, rizhao_date='')
    params = dict(initial['parameters'])
    with st.expander('固定参数与计算公式（可修改）'):
        st.caption('按你提供的参考图设定，不需每日重复填写；每次保存同时保留参数。税费为研究假设。')
        st.markdown('FOB =（原糖价格 + 升贴水）×22.046；CIF = [FOB×(1+保险费率)+海运费]×汇率。\n\n成本 = CIF×(1+关税)×(1+增值税)×(1+损耗)×(1+银行手续费率)×(1+代理费率)×旋光度系数 + 利息劳务费 + 加工费 + 其他费用。')
        columns = st.columns(3)
        for i, (field, value) in enumerate(params.items()):
            if field == 'loss_mode':
                params[field] = st.selectbox('损耗口径', ('成本加成', '实际损耗'), index=0 if value == '成本加成' else 1, key=key + ':loss-mode')
            else:
                percent = field in ('tariff_in', 'tariff_out', 'vat', 'loss', 'insurance_rate', 'bank_rate', 'agency_rate')
                v = columns[i % 3].number_input(('加工及国内运杂费' if field == 'processing' else LABELS[field]) + ('（%）' if percent else ''), value=float(value) * (100 if percent else 1), min_value=0., format='%.6f', key=key + ':' + field)
                params[field] = v / 100 if percent else v
        source = st.text_area('报价备注（选填）', initial['source'], key=key + ':source')
    payload = dict(schema_version=RESEARCH_VERSION, date=day.isoformat(), country=country, quote_mode=mode,
                   source=source or '人工录入升贴水及海运费', parameters=params, quotes=quotes, market=st.session_state[key + ':market'])
    # Validation before any network request prevents malformed user-controlled symbols.
    try:
        validate_monthly(dict(payload, source=source or '会话试算，来源待填写'))
    except (ValueError, TypeError, KeyError) as exc:
        st.info(str(exc))
        skeleton = dict(initial, market=None, quotes=[dict(r, quote=None) for r in initial['quotes']])
        st.iframe(monthly_html(monthly_rows(skeleton), country, day), height=650, width='stretch')
        return
    signature = hashlib.sha256(json.dumps([(r['period'], r['quote'], r['ice_contract'], r['sr_contract']) for r in quotes], allow_nan=False).encode()).hexdigest()
    if key + ':signature' not in st.session_state:
        st.session_state[key + ':signature'] = signature if current else None
    refresh = st.button('刷新原糖、郑糖与汇率', key=key + ':refresh', disabled=day.isoformat() != today())
    changed = st.session_state[key + ':signature'] != signature
    if refresh or (changed and any(r['quote'] is not None for r in quotes) and day.isoformat() == today()):
        st.session_state[key + ':market'] = None
        try:
            with st.spinner('获取具体合约价格和汇率…'):
                st.session_state[key + ':market'] = capture_monthly_market(day, quotes)
        except (ValueError, TypeError, KeyError):
            st.warning('行情获取或校验失败；会话旧行情已撤下，相关计算保持空值。')
    st.session_state[key + ':signature'] = signature
    payload['market'] = st.session_state[key + ':market']
    rows = monthly_rows(payload)
    st.iframe(monthly_html(rows, country, day), height=650, width='stretch')
    st.caption('配额内与配额外分别计算；保险按FOB×费率自动计入，无须逐月填保险金额。')
    market = payload['market']
    with st.expander('行情时间、来源与缺失原因'):
        if market:
            st.caption('获取时间：' + market['captured_at'] + '；报价最多7天。原糖收盘价标注实际交易日；原糖最新价时间为供应商更新时间。')
            for kind in ('ice', 'domestic'):
                items = {c: ice_quote(market, c) for c in market[kind]} if kind == 'ice' else market[kind]
                st.dataframe(pd.DataFrame([dict(合约=c, 价格=r.get('price'), 价格口径='收盘价' if r.get('time_kind') == 'daily_close' else '最新价', 来源=r.get('provider'), 来源时间=r.get('quoted_at')) for c, r in items.items()]), hide_index=True)
            if market['errors']: st.warning('；'.join(f'{c}: {e}' for c, e in market['errors'].items()))
            st.json(dict(在岸汇率曲线=market['fx_curve'], CFETS原始凭证=market['fx_raw']))
        else: st.info('尚无当日已存或新获取的行情；历史日期不获取当前价格。')
        st.dataframe(pd.DataFrame([dict(船期=r['period'], 状态=r['status']) for r in rows]), hide_index=True)
        st.dataframe(pd.DataFrame([dict(船期=r['period'], **_freight_source(r)) for r in rows]), hide_index=True)
    if st.button('保存当日报价与测算', key=key + ':save', disabled=not enabled):
        try:
            st.session_state[key + ':version'] = store.save_monthly(payload, expected_version=st.session_state[key + ':version'])
            records = store.monthly_records()
            st.success('月份报价已保存，行情、参数和来源随记录保留。')
        except (ValueError, TypeError, KeyError, sqlite3.Error) as exc: st.warning(str(exc))
    exported = monthly_table(rows)
    exported = pd.concat([exported, pd.DataFrame([_freight_source(r) for r in rows])], axis=1)
    for name, value in reversed([('报价日', day.isoformat()), ('国家', country), ('报价口径', mode), ('人工来源', source)]):
        exported.insert(0, name, value)
    st.download_button('导出月份盘面表 CSV', _csv(exported), 'sugar-monthly.csv', 'text/csv', key=key + ':export')
    observations = [dict(r, date=item['payload']['date']) for item in records
                    if item['payload']['country'] == country and item['payload']['date'] <= day.isoformat() and item['payload']['schema_version'] == RESEARCH_VERSION
                    for r in monthly_rows(item['payload'])]
    _seasonal_charts(observations, country, key)
    if observations:
        frame = pd.DataFrame(observations)
        st.subheader('历史报价与利润')
        recent = frame[frame.date.isin(sorted(frame.date.unique(), reverse=True)[:12])].copy()
        recent['船期月'] = recent.period.str[-2:] + '月'
        for metric, title in (('quote', '升贴水（美分/磅）'), ('freight', '海运费（美元/吨）'), ('margin_in', '配额内进口利润'), ('margin_out', '配额外进口利润')):
            with st.expander(title):
                st.dataframe(recent.pivot(index='date', columns='船期月', values=metric).sort_index(ascending=False), width='stretch')


def _seasonal_x(stamp, shipment_year):
    """Leap-safe day axis; each relative year has the same 366-day template."""
    return (stamp.year - shipment_year) * 366 + (date(2000, stamp.month, stamp.day) - date(2000, 1, 1)).days


@cache_computation
def build_sugar_seasonal_figure(observations, month, quota):
    from agri_research_agent.shared.chart_style import (CURRENT_YEAR_COLOR, CURRENT_LINE_WIDTH,
        HISTORY_LINE_WIDTH, GRID_COLOR, historical_year_color)
    metric = 'margin_out' if quota == '配额外' else 'margin_in'
    figure = go.Figure()
    selected = [r for r in observations if int(r['period'][-2:]) == month]
    periods = sorted({r['period'] for r in selected})
    for period in periods:
        shipment_year = int(period[:4])
        x, y, labels, last = [], [], [], None
        for row in sorted((r for r in selected if r['period'] == period), key=lambda r: r['date']):
            stamp = date.fromisoformat(row['date'])
            # Normalize relative to shipment year, retaining the original date in hover.
            anchor = _seasonal_x(stamp, shipment_year)
            if last and (stamp - last).days > 10:
                x.append(anchor); y.append(None); labels.append('')
            x.append(anchor); y.append(row[metric]); labels.append(row['date']); last = stamp
        latest = period == periods[-1]
        figure.add_trace(go.Scatter(x=x, y=y, customdata=labels, name=period[:4], mode='lines+markers', connectgaps=False,
            line=dict(color=CURRENT_YEAR_COLOR if latest else historical_year_color(shipment_year),
                      width=CURRENT_LINE_WIDTH if latest else HISTORY_LINE_WIDTH),
            hovertemplate='%{customdata}<br>%{y:.2f}元/吨<extra>%{fullData.name}年船期</extra>'))
    figure.update_layout(title=f'{month}月船期 · {quota}', height=280, yaxis_title='元/吨',
        plot_bgcolor='white', xaxis=dict(gridcolor=GRID_COLOR, tickmode='array',
            tickvals=[offset * 366 + (date(2000, m, 1) - date(2000, 1, 1)).days for offset in (-1, 0) for m in range(1, 13, 2)],
            ticktext=[f'{m:02}/01' for _ in (-1, 0) for m in range(1, 13, 2)]),
        yaxis=dict(gridcolor=GRID_COLOR, zeroline=True), legend=dict(orientation='h'),
        margin=dict(l=35, r=10, t=45, b=30))
    return figure


def _seasonal_charts(observations, country, key):
    st.subheader('白糖进口利润 · ' + country)
    quota = st.radio('利润口径', ('配额外', '配额内'), horizontal=True, key=key + ':chart-quota')
    if not observations:
        st.info('尚无新公式的已存历史；从实际录价日开始积累，缺失数据留空。')
    for start in range(1, 13, 3):
        for month, column in zip(range(start, start + 3), st.columns(3)):
            figure = build_sugar_seasonal_figure(observations, month, quota)
            with column:
                st.plotly_chart(figure, width='stretch', key=key + f':chart-{month}')


def _csv(frame):
    frame = frame.copy()
    for column in frame.columns:
        frame[column] = frame[column].map(lambda value: "'" + value if isinstance(value, str)
                                          and value.lstrip().startswith(("=", "+", "-", "@")) else value)
    return frame.to_csv(index=False).encode("utf-8-sig")



def render_sugar_import_profit_page():
    st.title('中国进口白糖盘面利润')
    try:
        path, history_root, authorize, enabled = storage_context()
        store = Store(path, authorize)
        _monthly(store, enabled, history_root / 'hisugar_freight')
        with st.expander('旧版自算记录（原公式，只读）'):
            old = store.records()
            from agri_research_agent.sugar_import_profit.model import MONTHLY_VERSION
            monthly = [r for r in store.monthly_records() if r['payload']['schema_version'] == MONTHLY_VERSION]
            st.caption('旧记录保留原参数和结果，不进入新公式的历史图。')
            st.download_button('导出旧版自算记录', json.dumps(dict(daily=old, monthly=monthly), ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'),
                'sugar-legacy-own-records.json', 'application/json')
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        st.warning('白糖进口利润数据暂不可用：' + str(exc))
