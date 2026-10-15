"""Brazil/Thailand sugar import-profit workbench; manual quotes, no HTTP collection."""
from __future__ import annotations
from datetime import date
import json
import sqlite3
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from agri_research_agent.sugar_import_profit.history import defaults, history_rows, load_sources
from agri_research_agent.sugar_import_profit.model import COUNTRIES, DATE_FIELDS, LABELS, NUMERIC, calculate, today, validate_inputs
from agri_research_agent.sugar_import_profit.runtime import storage_context
from agri_research_agent.sugar_import_profit.store import Store


def monthly_table(rows):
    return pd.DataFrame([dict(船期=r['period'], CNF报价=r['quote'], 原糖合约=r['ice_contract'],
        原糖价格=r['ice'], 郑糖合约=r['sr_contract'], 郑糖价格=r['sr'], 汇率=r['fx'],
        保险=r['insurance'], 配额内盘面榨利=r['margin_in'], 配额外盘面榨利=r['margin_out'],
        配额内不含保险试算=r['excluding_insurance_in'], 配额外不含保险试算=r['excluding_insurance_out'],
        状态=r['status']) for r in rows]).rename(columns={'原糖价格':'原糖（美分/磅）', '郑糖价格':'郑糖（元/吨）', '保险':'保险（美元/吨）'})


def monthly_html(rows):
    from html import escape
    frame = monthly_table(rows)
    head = ''.join('<th>' + escape(c) + '</th>' for c in frame.columns)
    body = []
    for row in frame.to_dict('records'):
        cells = []
        for key, v in row.items():
            if v is None or (isinstance(v, float) and pd.isna(v)): text = '--'
            elif isinstance(v, (int, float)): text = f'{v:.4f}' if key == '汇率' else f'{v:.2f}'
            else: text = str(v)
            cells.append('<td>' + escape(text) + '</td>')
        body.append('<tr>' + ''.join(cells) + '</tr>')
    return ('<style>body{font-family:Arial,"Microsoft YaHei",sans-serif;color:#17283f}'
        '.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:1500px}'
        'th{background:#ba2924;color:white;padding:15px 10px;font-size:14px;white-space:nowrap}'
        'td{border:1px solid #e5eaf1;text-align:center;padding:14px 8px;font-size:14px;white-space:nowrap}'
        'tr:nth-child(even){background:#f8fafc}</style><div class="scroll"><table><thead><tr>'
        + head + '</tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>')


def _monthly(store, enabled):
    import hashlib
    from agri_research_agent.sugar_import_profit.model import (
        MONTHLY_VERSION, MONTHLY_PARAMETERS, QUOTE_MODES, month_periods, default_mapping,
        monthly_rows, validate_monthly)
    from agri_research_agent.sugar_import_profit.runtime import capture_monthly_market
    st.caption('未来12个月船期 · 当月行表示次年同月 · 成本和利润为元/吨')
    controls = st.columns([1, 2])
    country = controls[0].radio('月份报价国家', COUNTRIES, horizontal=True, key='sugar-month-country')
    day = controls[1].date_input('报价业务日', date.fromisoformat(today()), max_value=date.fromisoformat(today()), key='sugar-month-day')
    key = f'sugar-month:{country}:{day}'
    records = store.monthly_records()
    current = next((r for r in records if r['payload']['date'] == day.isoformat() and r['payload']['country'] == country), None)
    if st.button('重新载入月份记录', key=key + ':reload'):
        for name in list(st.session_state):
            if name.startswith(key + ':') and name != key + ':reload': del st.session_state[name]
        st.rerun()
    if key + ':initial' not in st.session_state:
        payload = current['payload'] if current else dict(schema_version=MONTHLY_VERSION, date=day.isoformat(), country=country,
            quote_mode=QUOTE_MODES[0], source='', parameters=dict(MONTHLY_PARAMETERS), market=None,
            quotes=[dict(period=p, ice_contract=default_mapping(p)[0], sr_contract=default_mapping(p)[1],
                         quote=None, freight=None, insurance=None, rizhao=None, rizhao_date='') for p in month_periods(day)])
        st.session_state[key + ':initial'] = payload
        st.session_state[key + ':version'] = current['version'] if current else None
        st.session_state[key + ':market'] = payload['market']
    initial = st.session_state[key + ':initial']
    fields = dict(period='船期', quote='CNF报价', ice_contract='ICE合约', sr_contract='郑糖合约',
                  freight='海运费（美元/吨）', insurance='保险（美元/吨）', rizhao='日照现货（元/吨）', rizhao_date='日照报价日')
    with st.expander('录入升贴水、运保费与合约'):
        mode = st.selectbox('CNF报价口径', QUOTE_MODES, index=QUOTE_MODES.index(initial['quote_mode']) if initial['quote_mode'] in QUOTE_MODES else None,
            placeholder='先选择报价口径', key=key + ':mode')
        st.caption('默认升贴水加ICE后按旋光度换算，再单列运保费；含运费CNF升贴水不再加海运费；美元/吨CNF全价不重复加原糖价或运费。')
        st.caption('合约初始映射为研究假设：船期后首个ICE交割月；船期加2个月后首个郑糖1/5/9月。请按实际报价逐月修改并核对。')
        edited = st.data_editor(pd.DataFrame(initial['quotes']).sort_values('period').rename(columns=fields),
            hide_index=True, disabled=['船期'], num_rows='fixed', height=460, key=key + ':editor',
            column_config={name: st.column_config.NumberColumn(name, min_value=0. if field != 'quote' else None, format='%.4f')
                           for field, name in fields.items() if field in ('quote', 'freight', 'insurance', 'rizhao')})
    quotes = edited.rename(columns={v: k for k, v in fields.items()}).astype(object).where(pd.notna(edited.rename(columns={v: k for k, v in fields.items()})), None).to_dict('records')
    params = dict(initial['parameters'])
    with st.expander('税费、加工、损耗与来源'):
        st.caption('沿用原表的研究参数，可修改；参数不代表所有日期适用的政策。缺失保险与日照现货不自动填充。')
        columns = st.columns(3)
        for i, (field, value) in enumerate(params.items()):
            if field == 'loss_mode':
                params[field] = st.selectbox('损耗口径', ('成本加成', '实际损耗'), index=0 if value == '成本加成' else 1, key=key + ':loss-mode')
            else:
                percent = field in ('tariff_in', 'tariff_out', 'vat', 'loss')
                v = columns[i % 3].number_input(LABELS[field] + ('（%）' if percent else ''), value=float(value) * (100 if percent else 1), min_value=0., key=key + ':' + field)
                params[field] = v / 100 if percent else v
        source = st.text_area('CNF及人工参数来源', initial['source'], key=key + ':source')
    payload = dict(schema_version=MONTHLY_VERSION, date=day.isoformat(), country=country, quote_mode=mode,
                   source=source, parameters=params, quotes=quotes, market=st.session_state[key + ':market'])
    # Validation before any network request prevents malformed user-controlled symbols.
    try:
        checked = validate_monthly(dict(payload, source=source or '会话试算，来源待填写'))
    except (ValueError, TypeError, KeyError) as exc:
        st.info(str(exc))
        skeleton = dict(initial, market=None, quotes=[dict(r, quote=None) for r in initial['quotes']])
        st.iframe(monthly_html(monthly_rows(skeleton)), height=695, width='stretch')
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
    st.iframe(monthly_html(rows), height=695, width='stretch')
    st.caption('完整盘面榨利 = 郑糖具体合约价 − 含保险、税费、损耗和加工费的成本。不含保险试算仅用于初步比较，不是完整榨利。日照缺价不影响盘面计算。')
    market = payload['market']
    with st.expander('行情实际时间、来源和现货利润'):
        if market:
            st.caption('获取时间：' + market['captured_at'] + '；来源最新可用报价，最多7天；原糖时间为供应商更新时间。')
            for kind in ('ice', 'domestic'):
                st.dataframe(pd.DataFrame([dict(合约=c, 价格=r.get('price'), 来源=r.get('provider'), 来源时间=r.get('quoted_at')) for c, r in market[kind].items()]), hide_index=True)
            if market['errors']: st.warning('；'.join(f'{c}: {e}' for c, e in market['errors'].items()))
            st.json(dict(在岸汇率曲线=market['fx_curve'], CFETS原始凭证=market['fx_raw']))
        else: st.info('尚无当日已存或新获取的行情；历史日期不获取当前价格。')
        st.dataframe(pd.DataFrame([dict(船期=r['period'], 日照现货=r['rizhao'], 实际报价日=r.get('rizhao_date'),
            配额内现货利润=r['spot_in'], 配额外现货利润=r['spot_out']) for r in rows]), hide_index=True)
    if st.button('保存月份报价与计算行情', key=key + ':save', disabled=not enabled):
        try:
            st.session_state[key + ':version'] = store.save_monthly(payload, expected_version=st.session_state[key + ':version'])
            st.success('月份报价已保存，行情、参数和来源随记录保留。')
        except (ValueError, TypeError, KeyError, sqlite3.Error) as exc: st.warning(str(exc))
    exported = monthly_table(rows)
    for name, value in reversed([('报价日', day.isoformat()), ('国家', country), ('报价口径', mode), ('人工来源', source)]):
        exported.insert(0, name, value)
    st.download_button('导出月份盘面表 CSV', _csv(exported), 'sugar-monthly.csv', 'text/csv', key=key + ':export')
    observations = [dict(r, date=item['payload']['date']) for item in records
                    if item['payload']['country'] == country and item['payload']['date'] <= day.isoformat()
                    for r in monthly_rows(item['payload'])]
    if not observations:
        st.info('暂无已保存月份历史；从真实录价日期开始积累，不将原钢联单日利润拆成12个月。')
        return
    frame = pd.DataFrame(observations)
    recent = frame[frame.date.isin(sorted(frame.date.unique(), reverse=True)[:12])].copy()
    recent['船期月'] = recent.period.str[-2:] + '月'
    for metric, title in (('quote', '已存月份CNF'), ('margin_in', '配额内盘面榨利'), ('margin_out', '配额外盘面榨利')):
        st.markdown('#### ' + title)
        st.dataframe(recent.pivot(index='date', columns='船期月', values=metric).sort_index(ascending=False), width='stretch')
    st.caption('月份历史使用当时保存的报价、合约、参数和行情；不含保险试算不进入完整榨利历史。')
    if not frame[['margin_in', 'margin_out']].notna().any().any():
        st.info('暂无完整盘面榨利观测，月份图保持空白。')
        return
    st.subheader('按船期月份的历史盘面榨利')
    quota = st.radio('月份图配额', ('配额内', '配额外'), horizontal=True, key=key + ':chart-quota')
    metric = 'margin_in' if quota == '配额内' else 'margin_out'
    for start in range(1, 13, 3):
        for month, column in zip(range(start, start + 3), st.columns(3)):
            with column:
                figure = go.Figure()
                selected = frame[frame.period.str[-2:] == f'{month:02}']
                for period, group in selected.groupby('period'):
                    x, y, last = [], [], None
                    for row in group.sort_values('date').to_dict('records'):
                        stamp = date.fromisoformat(row['date'])
                        if last and (stamp - last).days > 10:
                            x.append(row['date']); y.append(None)
                        x.append(row['date']); y.append(row[metric]); last = stamp
                    figure.add_trace(go.Scatter(x=x, y=y, name=period, mode='lines+markers', connectgaps=False))
                figure.update_layout(title=f'{month}月船期 · {quota}', height=280, yaxis_title='元/吨',
                                     margin=dict(l=35, r=10, t=45, b=30))
                st.plotly_chart(figure, width='stretch', key=key + f':chart-{month}')


def _csv(frame):
    frame = frame.copy()
    for column in frame.columns:
        frame[column] = frame[column].map(lambda value: "'" + value if isinstance(value, str)
                                          and value.lstrip().startswith(("=", "+", "-", "@")) else value)
    return frame.to_csv(index=False).encode("utf-8-sig")


def _charts(rows):
    for country, columns in zip(COUNTRIES, (st.columns(2), st.columns(2))):
        selected = [r for r in rows if r["country"] == country]
        for quota, column in zip(("in", "out"), columns):
            title = f"{country}配额{'内' if quota == 'in' else '外'}进口利润"
            with column:
                figure = go.Figure(go.Scatter(x=[r["date"] for r in selected],
                    y=[r["profit_" + quota] for r in selected], mode="lines", connectgaps=False,
                    line=dict(width=2.4, color="#2457A7" if quota == "in" else "#B94840")))
                figure.update_layout(title=title, height=340, margin=dict(l=35, r=10, t=50, b=30),
                                     yaxis_title="元/吨", hovermode="x unified")
                st.plotly_chart(figure, use_container_width=True, key="sugar-profit-chart-" + country + quota)


def _history(sources, store, enabled):
    source = st.radio("数据来源", ("钢联历史利润", "本人自算记录"), horizontal=True, key="sugar-profit-source")
    a, b = st.columns(2)
    start = a.date_input("起始日", date(2020, 7, 2), key="sugar-profit-from")
    end = b.date_input("截止日", date.fromisoformat(today()), key="sugar-profit-to")
    if start > end:
        st.warning("起始日期不能晚于截止日期")
        return
    if source == "钢联历史利润":
        rows = history_rows(sources, start.isoformat(), end.isoformat())
        st.caption("钢联原表历史利润，保留当时口径与空值；不使用当前参数重算。")
    else:
        rows = [dict(date=r["date"], country=r["country"], **{k: r["results"][k] for k in ("profit_in", "profit_out")})
                for r in store.records() if start.isoformat() <= r["date"] <= end.isoformat()]
        rows.sort(key=lambda row: (row["date"], row["country"]))
        if not rows:
            st.info("尚无本人自算记录，请到当日计算填写并保存。")
    _charts(rows)
    frame = pd.DataFrame(rows, columns=["date", "country", "profit_in", "profit_out"])
    st.download_button("导出历史 CSV", _csv(frame), "sugar-profit-history.csv", "text/csv", key="sugar-profit-history-export")
    note_token = "sugar-profit-note-version"
    if note_token not in st.session_state:
        st.session_state[note_token] = store.setting("research-note")
    old = st.session_state[note_token]
    with st.form("sugar-profit-note-form"):
        text = st.text_area("研究判断", (old or {}).get("text", ""))
        note_date = st.date_input("笔记日期", date.fromisoformat((old or {}).get("date", today())))
        save = st.form_submit_button("保存研究判断", disabled=not enabled)
    if save:
        try:
            st.session_state[note_token] = store.save_note(dict(text=text, date=note_date.isoformat()), old)
            st.success("研究判断已保存")
        except ValueError as exc:
            st.warning(str(exc))


def _calculator(sources, store, enabled):
    country = st.radio("计算国家", COUNTRIES, horizontal=True, key="sugar-profit-country")
    day = st.date_input("计算日期", date.fromisoformat(today()), key="sugar-profit-day").isoformat()
    current = next((r for r in store.records() if r["country"] == country and r["date"] == day), None)
    initial = current["inputs"] if current else defaults(sources, country, day)
    # Freeze the version that populated this session's inputs. A later rerun must
    # not silently adopt a concurrent writer's token while preserving stale widgets.
    group = "sugar-profit-input:" + country + ":" + day
    if st.button("重新载入当日记录", key=group + ":reload"):
        for key in list(st.session_state):
            if key.startswith(group + ":") and key != group + ":reload":
                del st.session_state[key]
        st.rerun()
    token = group + ":version"
    if token not in st.session_state:
        st.session_state[token] = current["version"] if current else None
    raw = dict(date=day, example=False)
    st.caption("原糖、升贴水：美分/磅；海运费、保险费：美元/吨；成本、售价、利润：元/吨。行情由人工填写。")
    with st.container(border=True):
        percent_fields = {"tariff_in", "tariff_out", "vat", "loss"}
        for index, key in enumerate(NUMERIC):
            if index % 3 == 0:
                columns = st.columns(3)
            label = LABELS[key] + ("（%）" if key in percent_fields else "")
            value = initial.get(key)
            if value is not None:
                value = float(value) * (100 if key in percent_fields else 1)
            entered = columns[index % 3].number_input(label, value=value,
                format="%.4f", key=group + ":" + key)
            raw[key] = entered / 100 if entered is not None and key in percent_fields else entered
        raw["loss_mode"] = st.selectbox("损耗口径", ("成本加成", "实际损耗"),
            index=0 if initial.get("loss_mode") == "成本加成" else 1, key=group + ":loss-mode")
        raw["contract"] = st.text_input("ICE 原糖合约", initial.get("contract", ""), key=group + ":contract")
        for index, key in enumerate(DATE_FIELDS[1:]):
            if index % 3 == 0:
                dates = st.columns(3)
            value = initial.get(key)
            selected = dates[index % 3].date_input(LABELS[key], date.fromisoformat(value) if value else None,
                                                 key=group + ":" + key)
            raw[key] = selected.isoformat() if selected else ""
        raw["source_note"] = st.text_area("价格及参数来源", initial.get("source_note", ""), key=group + ":source-note")
    st.caption("离岸价 =（原糖价＋升贴水）×旋光度系数×换算系数；到岸价 =（离岸价＋运费＋保险）×在岸人民币汇率。默认系数与加成来自中哲参考图，实际参数可单独调整。")
    try:
        result = calculate(validate_inputs(raw))
    except ValueError as exc:
        st.info(str(exc))
        return
    result_frame = pd.DataFrame([
        {"项目": "完税成本", "配额内": result["taxed_in"], "配额外": result["taxed_out"]},
        {"项目": "加工后成本", "配额内": result["cost_in"], "配额外": result["cost_out"]},
        {"项目": "进口利润", "配额内": result["profit_in"], "配额外": result["profit_out"]}])
    st.dataframe(result_frame, hide_index=True, use_container_width=True)
    st.caption("报价日期：" + "；".join(LABELS[k] + " " + raw[k] for k in DATE_FIELDS[1:]))
    pending_key = group + ":pending-revision"
    if st.button("保存当日计算", disabled=not enabled, key=group + ":save"):
        try:
            saved = store.save(country, raw, expected_version=st.session_state[token])
            if saved["status"] == "conflict":
                st.session_state[pending_key] = saved
            else:
                st.session_state.pop(pending_key, None)
                st.session_state[token] = saved["version"]
                st.success("相同记录已保存，没有重复新增" if saved["status"] == "unchanged" else "当日计算已保存")
        except ValueError as exc:
            st.warning(str(exc))
    pending = st.session_state.get(pending_key)
    if pending and pending["new"] != validate_inputs(raw):
        st.session_state.pop(pending_key, None)
        pending = None
    if pending:
        st.warning("当日已有记录。请核对差异；修订会保留旧版本。")
        differences = [{"项目": LABELS.get(k, k), "旧值": str(v), "新值": str(pending["new"].get(k))}
                       for k, v in pending["old"].items() if v != pending["new"].get(k)]
        st.dataframe(pd.DataFrame(differences), hide_index=True)
        if st.button("确认以修订保存", disabled=not enabled, key=group + ":revise"):
            try:
                saved = store.save(country, pending["new"], expected_version=st.session_state[token], revise=True)
                st.session_state.pop(pending_key, None)
                st.session_state[token] = saved["version"]
                st.success("修订已保存，旧版本保留")
            except ValueError as exc:
                st.warning(str(exc))


def render_sugar_import_profit_page():
    st.title("白糖进口利润")
    try:
        path, history_root, authorize, enabled = storage_context()
        store = Store(path, authorize)
        st.caption("巴西／泰国 · 月份CNF录价与盘面榨利 · 钢联历史利润与原有自算记录")
        monthly_tab, history_tab, calculate_tab, records_tab = st.tabs(("月份盘面榨利", "历史利润", "当日计算", "自算记录"))
        with monthly_tab:
            _monthly(store, enabled)
        try:
            sources, manifest = load_sources(history_root)
        except (OSError, ValueError, KeyError):
            sources = None
        with history_tab:
            if sources: _history(sources, store, enabled)
            else: st.info('钢联历史文件尚未提供或未通过校验；月份录价可独立使用。')
        with calculate_tab:
            if sources: _calculator(sources, store, enabled)
            else: st.info('原单日现货计算需要原历史来源；请使用月份盘面榨利。')
        with records_tab:
            records = store.records()
            rows = [dict(日期=r["date"], 国家=r["country"], 合约=r["inputs"]["contract"],
                         配额内利润=r["results"]["profit_in"], 配额外利润=r["results"]["profit_out"], 保存时间=r["saved_at"]) for r in records]
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
                with st.expander("查看完整报价、参数与来源"):
                    index = st.selectbox("选择记录", range(len(records)),
                                         format_func=lambda i: records[i]["date"] + " · " + records[i]["country"])
                    st.json(records[index])
            else:
                st.info("尚无自算记录")
            st.download_button("导出完整自算记录", json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"),
                               "sugar-own-records.json", "application/json", key="sugar-own-records-export")
            st.download_button("导出自算记录 CSV", _csv(pd.DataFrame(rows)), "sugar-own-records.csv", "text/csv", key="sugar-own-records-csv")
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        st.warning("白糖进口利润数据暂不可用：" + str(exc))
