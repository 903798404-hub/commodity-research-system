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
        sources, manifest = load_sources(history_root)
        store = Store(path, authorize)
        st.caption("巴西／泰国 · 钢联历史利润与本人自算记录 · 行情自动接入暂未启用")
        st.caption("钢联历史截至 " + max(sources[c]["rows"][-1][0] for c in COUNTRIES))
        history_tab, calculate_tab, records_tab = st.tabs(("历史利润", "当日计算", "自算记录"))
        with history_tab:
            _history(sources, store, enabled)
        with calculate_tab:
            _calculator(sources, store, enabled)
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
