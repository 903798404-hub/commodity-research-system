"""Shared, presentation-only primitives for the Streamlit research workbench."""
from __future__ import annotations

import os
from html import escape
from typing import TYPE_CHECKING, Iterable
from urllib.parse import quote

import streamlit as st

if TYPE_CHECKING:
    from home import HomeModule, ModuleStatus


STATUS_CLASS = {
    "success": "is-success",
    "warning": "is-warning",
    "info": "is-info",
    "external": "is-info",
    "unavailable": "is-error",
}


def _icon_markup(name: str, css_class: str = "agri-icon") -> str:
    """Return a CSS-drawn icon that stays compatible with Streamlit HTML sanitization."""
    class_name = css_class if css_class != "agri-icon" else f"agri-icon-{escape(name)}"
    return f'<span class="{class_name}" aria-hidden="true"></span>'


def inject_workspace_theme() -> None:
    """Install styling for the new homepage and custom sidebar fragments only."""
    st.html(
        """
<style>
.agri-home-header { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 24px; align-items: end; padding: 5px 0 22px; border-bottom: 1px solid #DCE4EE; }
.agri-home-kicker { color: #4D6B8E; font-size: 11px; font-weight: 800; letter-spacing: .12em; }
.agri-home-title { margin: 7px 0 6px; color: #15263A; font-size: clamp(32px, 3.1vw, 46px); letter-spacing: -.04em; line-height: 1.16; font-weight: 780; }
.agri-home-description { max-width: 720px; margin: 0; color: #5D6F82; font-size: 15px; line-height: 1.65; }
.agri-home-meta { display: grid; grid-template-columns: auto auto; gap: 6px 14px; align-items: center; color: #5D6F82; font-size: 12px; text-align: right; }
.agri-home-meta-note { grid-column: 1 / -1; color: #738396; }
.agri-section { display: flex; align-items: end; justify-content: space-between; gap: 20px; margin: 24px 0 12px; }
.agri-section h2 { margin: 0; color: #1A2D43; font-size: 20px; line-height: 1.35; letter-spacing: -.02em; }
.agri-section p { margin: 0; color: #718195; font-size: 13px; line-height: 1.5; text-align: right; }
.agri-card-area { container-type: inline-size; width: 100%; }
.agri-card-collection { display: grid; grid-template-columns: 1fr; gap: 16px; width: 100%; }
.agri-card { box-sizing: border-box; display: flex; min-width: 0; min-height: 290px; flex-direction: column; padding: 19px; border: 1px solid #DCE5EF; border-radius: 14px; background: #FFF; box-shadow: 0 5px 16px rgba(22, 43, 68, .055); }
.agri-card-top { display: flex; gap: 12px; align-items: center; }
.agri-icon-chart, .agri-icon-quote, .agri-icon-leaf, .agri-icon-institution, .agri-icon-globe, .agri-icon-monitor { position: relative; display: inline-flex; width: 42px; height: 42px; flex: 0 0 42px; box-sizing: border-box; overflow: hidden; border: 1px solid #D7E5F5; border-radius: 50%; background: #F1F6FC; color: #2D629D; }
.agri-icon-chart::before, .agri-icon-chart::after, .agri-icon-quote::before, .agri-icon-quote::after, .agri-icon-leaf::before, .agri-icon-leaf::after, .agri-icon-institution::before, .agri-icon-institution::after, .agri-icon-globe::before, .agri-icon-globe::after, .agri-icon-monitor::before, .agri-icon-monitor::after { position: absolute; box-sizing: border-box; content: ""; }
.agri-icon-chart::before { left: 11px; bottom: 11px; width: 19px; height: 14px; border-bottom: 2px solid currentColor; border-left: 2px solid currentColor; }
.agri-icon-chart::after { left: 14px; top: 14px; width: 16px; height: 11px; border-top: 2px solid currentColor; transform: skewY(-28deg) rotate(-10deg); }
.agri-icon-quote::before { left: 12px; top: 10px; width: 16px; height: 21px; border: 2px solid currentColor; border-radius: 3px; }
.agri-icon-quote::after { left: 16px; top: 15px; width: 9px; height: 2px; background: currentColor; box-shadow: 0 5px 0 currentColor, 0 10px 0 currentColor; }
.agri-icon-leaf::before { left: 12px; top: 9px; width: 17px; height: 20px; border: 2px solid currentColor; border-radius: 100% 0 100% 0; transform: rotate(-28deg); }
.agri-icon-leaf::after { left: 20px; top: 17px; width: 2px; height: 15px; background: currentColor; transform: rotate(42deg); }
.agri-icon-institution::before { left: 10px; top: 10px; width: 0; height: 0; border-right: 11px solid transparent; border-bottom: 8px solid currentColor; border-left: 11px solid transparent; }
.agri-icon-institution::after { left: 11px; top: 19px; width: 20px; height: 10px; border-top: 2px solid currentColor; border-bottom: 2px solid currentColor; background: repeating-linear-gradient(90deg, currentColor 0 2px, transparent 2px 6px); }
.agri-icon-globe::before { left: 10px; top: 10px; width: 20px; height: 20px; border: 2px solid currentColor; border-radius: 50%; }
.agri-icon-globe::after { left: 13px; top: 19px; width: 14px; height: 2px; background: currentColor; box-shadow: 0 -5px 0 currentColor, 0 5px 0 currentColor; }
.agri-icon-monitor::before { left: 10px; top: 11px; width: 21px; height: 14px; border: 2px solid currentColor; border-radius: 3px; }
.agri-icon-monitor::after { left: 16px; top: 28px; width: 9px; height: 2px; background: currentColor; box-shadow: 3px -10px 0 -0.4px currentColor; }
.agri-card-title-group { min-width: 0; }
.agri-card h3 { margin: 0; color: #172C43; font-size: 17px; line-height: 1.35; }
.agri-card-kicker { margin-top: 2px; color: #718195; font-size: 12px; }
.agri-card-description { min-height: 45px; margin: 17px 0 12px; color: #506277; font-size: 13px; line-height: 1.62; }
.agri-status { display: inline-flex; width: fit-content; align-items: center; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 750; white-space: nowrap; }
.agri-status.is-success { background: #E9F4EE; color: #28714F; }
.agri-status.is-warning { background: #FCF2E3; color: #A96816; }
.agri-status.is-error { background: #FBEDED; color: #B6433D; }
.agri-status.is-info { background: #EAF2FC; color: #356CA8; }
.agri-card-rule { width: 100%; height: 1px; margin: 13px 0 11px; background: #E6ECF3; }
.agri-card-detail { margin: 4px 0 0; color: #66798D; font-size: 12px; line-height: 1.5; }
.agri-card-detail strong { color: #42566C; font-weight: 700; }
.agri-card-action { display: inline-flex; width: fit-content; min-height: 34px; margin-top: auto; padding: 0 14px; align-self: flex-end; align-items: center; justify-content: center; border-radius: 7px; background: #245B97; color: #FFF !important; font-size: 13px; font-weight: 750; text-decoration: none !important; }
.agri-card-action:hover { background: #1C4879; color: #FFF !important; }
.agri-card-action.is-disabled { cursor: not-allowed; background: #E7EDF4; color: #8A99A9 !important; }
.agri-attention-collection { display: flex; flex-wrap: wrap; gap: 12px; }
.agri-attention { box-sizing: border-box; flex: 1 1 330px; min-width: 0; padding: 13px 15px; border: 1px solid #F0D7AF; border-left: 4px solid #C68B37; border-radius: 10px; background: #FFF9F0; }
.agri-attention strong { color: #875816; font-size: 13px; }
.agri-attention p { margin: 4px 0 0; color: #6E6048; font-size: 12px; line-height: 1.55; }
.agri-status-table { overflow: hidden; width: 100%; border: 1px solid #DCE5EF; border-radius: 12px; background: #FFF; }
.agri-status-table-head, .agri-status-row { display: grid; grid-template-columns: minmax(150px, 1.25fr) minmax(110px, .82fr) minmax(150px, 1.15fr) minmax(90px, .72fr); gap: 16px; align-items: center; }
.agri-status-table-head { padding: 10px 16px; border-bottom: 1px solid #DCE5EF; background: #F5F8FB; color: #708095; font-size: 11px; font-weight: 800; letter-spacing: .04em; }
.agri-status-row { padding: 12px 16px; border-bottom: 1px solid #E8EEF4; color: #5D6E81; font-size: 12px; }
.agri-status-row:last-child { border-bottom: 0; }
.agri-status-row-title { color: #20364E; font-weight: 750; }
.agri-status-row-source { color: #738396; font-size: 11px; }
.agri-footer { margin: 27px 0 4px; padding-top: 14px; border-top: 1px solid #DCE4EE; color: #748396; font-size: 12px; line-height: 1.6; }
.agri-sidebar { margin: -1rem -1rem 0; font-family: inherit; }
.agri-sidebar-brand { display: flex; gap: 10px; align-items: center; padding: 19px 16px 17px; background: #123A67; color: #FFF; }
.agri-sidebar-brand-icon { position: relative; display: inline-flex; width: 30px; height: 30px; box-sizing: border-box; border: 1px solid rgba(255,255,255,.35); border-radius: 9px; background: rgba(255,255,255,.12); }
.agri-sidebar-brand-icon::before { position: absolute; left: 8px; top: 6px; width: 12px; height: 15px; border: 1.5px solid #FFF; border-radius: 100% 0 100% 0; content: ""; transform: rotate(-30deg); }
.agri-sidebar-brand-icon::after { position: absolute; left: 15px; top: 15px; width: 1.5px; height: 10px; background: #FFF; content: ""; transform: rotate(38deg); }
.agri-sidebar-brand strong { display: block; font-size: 15px; letter-spacing: -.01em; }
.agri-sidebar-brand span { display: block; margin-top: 2px; color: #C8DBEE; font-size: 10px; letter-spacing: .08em; }
.agri-nav { padding: 14px 10px 4px; }
.agri-nav-group { margin: 0 0 14px; }
.agri-nav-group-title { margin: 0 0 5px 6px; color: #7B8BA0; font-size: 10px; font-weight: 800; letter-spacing: .1em; }
.agri-nav-link { display: flex; min-height: 34px; align-items: center; margin: 2px 0; padding: 0 9px; border-left: 3px solid transparent; border-radius: 7px; color: #53677E !important; font-size: 13px; font-weight: 650; text-decoration: none !important; }
.agri-nav-link:hover { background: #EDF4FB; color: #204E82 !important; }
.agri-nav-link.is-active { border-left-color: #2870B7; background: #E8F1FB; color: #174F86 !important; font-weight: 800; }
.agri-nav-link.is-disabled { cursor: not-allowed; color: #97A4B3 !important; }
.agri-sidebar-system { margin: 18px 10px 0; padding: 12px 8px 16px; border-top: 1px solid #DDE6EF; color: #748397; font-size: 11px; line-height: 1.65; }
.agri-sidebar-system strong { color: #486077; font-weight: 750; }
@container (min-width: 520px) {
  .agri-card-collection { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@container (min-width: 600px) {
  .agri-card-collection { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 720px) {
  .agri-home-header { grid-template-columns: 1fr; gap: 13px; padding-bottom: 17px; }
  .agri-home-title { font-size: clamp(29px, 8.8vw, 37px); white-space: nowrap; }
  .agri-home-meta { justify-content: start; grid-template-columns: auto auto; text-align: left; }
  .agri-section { align-items: start; flex-direction: column; gap: 4px; margin-top: 21px; }
  .agri-section p { text-align: left; }
  .agri-card-area { container-type: normal; }
  .agri-card-collection { grid-template-columns: 1fr; }
  .agri-card { min-height: 0; }
  .agri-card-description { min-height: 0; }
  .agri-status-table-head { display: none; }
  .agri-status-row { grid-template-columns: 1fr; gap: 4px; padding: 13px 15px; }
  .agri-status-row > div::before { display: block; margin-bottom: 1px; color: #8A98A9; font-size: 10px; font-weight: 800; letter-spacing: .06em; content: attr(data-label); }
  .agri-status-row .agri-status { margin-top: 1px; }
}
</style>
        """
    )


def _status_markup(state: str, label: str) -> str:
    css_class = STATUS_CLASS.get(state, "is-info")
    return f'<span class="agri-status {css_class}">{escape(label)}</span>'


def render_sidebar_navigation(
    groups: Iterable[tuple[str, Iterable[tuple[str, str, str | None]]]], selected_page: str
) -> None:
    """Render a stable HTML navigation shell that delegates routing to query params."""
    group_markup: list[str] = []
    for group_title, items in groups:
        links: list[str] = []
        for label, target, external_env in items:
            if external_env:
                external_url = os.getenv(external_env, "").strip()
                if external_url:
                    links.append(
                        f'<a class="agri-nav-link" href="{escape(external_url, quote=True)}" '
                        'target="_blank" rel="noopener noreferrer">'
                        f'{escape(label)}</a>'
                    )
                else:
                    links.append(f'<span class="agri-nav-link is-disabled">{escape(label)}（暂不可用）</span>')
                continue
            links.append(
                '<a class="agri-nav-link {active}" href="?workspace_page={target}">{label}</a>'.format(
                    active="is-active" if target == selected_page else "",
                    target=escape(quote(target), quote=True),
                    label=escape(label),
                )
            )
        group_markup.append(
            f'<section class="agri-nav-group"><div class="agri-nav-group-title">{escape(group_title)}</div>{"".join(links)}</section>'
        )

    environment = os.getenv("APP_ENV", "").strip() or "本地开发环境"
    st.html(
        f'''<aside class="agri-sidebar">
  <header class="agri-sidebar-brand">{_icon_markup("brand", "agri-sidebar-brand-icon")}
    <div><strong>农产品研究工作台</strong><span>AGRICULTURAL RESEARCH</span></div>
  </header>
  <nav class="agri-nav" aria-label="研究工作台导航">{"".join(group_markup)}</nav>
  <footer class="agri-sidebar-system"><strong>{escape(environment)}</strong><br>应用运行正常</footer>
</aside>'''
    )


def render_home_header(
    *,
    title: str,
    description: str,
    date_text: str,
    state: str,
    state_label: str,
    update_hint: str,
) -> None:
    st.html(
        f'''<header class="agri-home-header">
  <div><div class="agri-home-kicker">AGRICULTURAL RESEARCH WORKBENCH</div>
    <h1 class="agri-home-title">{escape(title)}</h1><p class="agri-home-description">{escape(description)}</p></div>
  <div class="agri-home-meta"><span>{escape(date_text)}</span>{_status_markup(state, state_label)}
    <span class="agri-home-meta-note">{escape(update_hint)}</span></div>
</header>'''
    )


def render_section_heading(title: str, description: str) -> None:
    st.html(
        f'<section class="agri-section"><h2>{escape(title)}</h2><p>{escape(description)}</p></section>'
    )


def render_dashboard_card(module: HomeModule, status: ModuleStatus) -> str:
    """Return one complete responsive homepage card with an accessible inline SVG icon."""
    card_icons = {
        "spreads_dashboard": "chart",
        "basis_domestic": "quote",
        "soybean_crop_progress": "leaf",
        "crop_weather": "leaf",
        "usda_dashboard": "institution",
        "oil_world_dashboard": "globe",
        "status": "monitor",
    }
    if module.destination_page:
        action = (
            f'<a class="agri-card-action" href="?home_target={escape(module.module_id, quote=True)}">进入模块</a>'
        )
    elif module.external_env and (external_url := os.getenv(module.external_env, "").strip()):
        action = (
            f'<a class="agri-card-action" href="{escape(external_url, quote=True)}" '
            'target="_blank" rel="noopener noreferrer">打开独立应用</a>'
        )
    else:
        action = '<span class="agri-card-action is-disabled" aria-disabled="true">暂不可用</span>'

    return f'''<article class="agri-card">
  <div class="agri-card-top">{_icon_markup(card_icons[module.module_id])}
    <div class="agri-card-title-group"><h3>{escape(module.title)}</h3><div class="agri-card-kicker">{escape(module.update_mode)}</div></div></div>
  <p class="agri-card-description">{escape(module.description)}</p>
  {f'<p class="agri-card-detail">{escape(module.coverage_hint)}</p>' if module.coverage_hint else ''}
  {_status_markup(status.state, status.label)}
  <div class="agri-card-rule"></div>
  <p class="agri-card-detail"><strong>{escape(status.latest_value)}</strong></p>
  <p class="agri-card-detail">{escape(status.detail)}</p>
  {action}
</article>'''


def render_attention_items(items: Iterable[tuple[str, str]]) -> None:
    markup = "".join(
        f'<aside class="agri-attention"><strong>{escape(title)}</strong><p>{escape(message)}</p></aside>'
        for title, message in items
    )
    st.html(f'<section class="agri-attention-collection">{markup}</section>')


def render_status_overview(rows: list[tuple[str, str, str, ModuleStatus]]) -> None:
    body = "".join(
        f'''<article class="agri-status-row">
  <div class="agri-status-row-title" data-label="模块">{escape(title)}<div class="agri-status-row-source">{escape(source)}</div></div>
  <div data-label="更新方式">{escape(update_mode)}</div>
  <div data-label="最新数据">{escape(status.latest_value)}</div>
  <div data-label="状态">{_status_markup(status.state, status.label)}</div>
</article>'''
        for title, source, update_mode, status in rows
    )
    st.html(
        f'''<section class="agri-status-table"><div class="agri-status-table-head"><span>模块</span><span>更新方式</span><span>最新数据</span><span>状态</span></div>{body}</section>'''
    )


def render_home_footer(message: str) -> None:
    st.html(f'<footer class="agri-footer">{escape(message)}</footer>')
