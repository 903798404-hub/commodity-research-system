"""Shared, presentation-only primitives for the Streamlit research workbench."""
from __future__ import annotations

import os
from html import escape
from typing import TYPE_CHECKING, Iterable
from urllib.parse import quote

import streamlit as st

if TYPE_CHECKING:
    from navigation import NavigationGroup, NavigationItem


def _icon_markup(name: str, css_class: str = "agri-icon") -> str:
    """Return a CSS-drawn icon that stays compatible with Streamlit HTML sanitization."""
    class_name = css_class if css_class != "agri-icon" else f"agri-icon-{escape(name)}"
    return f'<span class="{class_name}" aria-hidden="true"></span>'


def inject_workspace_theme() -> None:
    """Install styling for the new homepage and custom sidebar fragments only."""
    st.html(
        """
<style>
.agri-home-header { padding: 5px 0 22px; border-bottom: 1px solid #DCE4EE; }
.agri-home-kicker { color: #4D6B8E; font-size: 11px; font-weight: 800; letter-spacing: .12em; }
.agri-home-title { margin: 7px 0 8px; color: #15263A; font-size: clamp(32px, 3.1vw, 46px); letter-spacing: -.04em; line-height: 1.16; font-weight: 780; }
.agri-home-meta { margin: 0; color: #65788C; font-size: 13px; line-height: 1.65; text-align: left; }
.agri-home-researcher { color: #40566D; font-size: 13px; font-weight: 700; }
.agri-home-contact { color: #7A8999; }
.agri-section { display: flex; align-items: end; justify-content: space-between; gap: 20px; margin: 24px 0 12px; }
.agri-section h2 { margin: 0; color: #1A2D43; font-size: 20px; line-height: 1.35; letter-spacing: -.02em; }
.agri-section p { margin: 0; color: #718195; font-size: 13px; line-height: 1.5; text-align: right; }
.agri-card-area { container-type: inline-size; width: 100%; }
.agri-card-collection { display: grid; grid-template-columns: 1fr; gap: 16px; width: 100%; }
.agri-navigation-group { margin: 14px 0 0; }
.agri-navigation-group-title { margin: 0 0 10px; padding-left: 10px; border-left: 3px solid #2D629D; color: #234F7D; font-size: 18px; font-weight: 800; line-height: 1.35; letter-spacing: -.01em; }
.agri-card { box-sizing: border-box; display: flex; min-width: 0; min-height: 200px; flex-direction: column; padding: 19px; border: 1px solid #DCE5EF; border-radius: 14px; background: #FFF; box-shadow: 0 5px 16px rgba(22, 43, 68, .055); }
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
.agri-card-description { min-height: 44px; margin: 17px 0 10px; color: #506277; font-size: 13px; line-height: 1.62; }
.agri-card-detail { min-height: 30px; margin: 0 0 10px; color: #78889A; font-size: 12px; line-height: 1.55; }
.agri-card-detail-label { margin-right: 4px; }
.agri-card-keyword { color: #354C64; font-weight: 650; }
.agri-card-action { display: inline-flex; width: fit-content; min-height: 34px; margin-top: auto; padding: 0 14px; align-self: flex-end; align-items: center; justify-content: center; border-radius: 7px; background: #245B97; color: #FFF !important; font-size: 13px; font-weight: 750; text-decoration: none !important; }
.agri-card-action:hover { background: #1C4879; color: #FFF !important; }
.agri-card-action.is-disabled { cursor: not-allowed; background: #E7EDF4; color: #8A99A9 !important; }
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
  .agri-home-header { padding-bottom: 17px; }
  .agri-home-title { font-size: clamp(29px, 8.8vw, 37px); white-space: nowrap; }
  .agri-section { align-items: start; flex-direction: column; gap: 4px; margin-top: 21px; }
  .agri-section p { text-align: left; }
  .agri-card-area { container-type: normal; }
  .agri-card-collection { grid-template-columns: 1fr; }
  .agri-card { min-height: 0; }
  .agri-card-description { min-height: 0; }
}
</style>
        """
    )


def render_sidebar_navigation(
    groups: Iterable[NavigationGroup], selected_page: str
) -> None:
    """Render a stable HTML navigation shell that delegates routing to query params."""
    group_markup: list[str] = []
    for group in groups:
        links: list[str] = []
        for item in group.items:
            if item.external_env:
                external_url = os.getenv(item.external_env, "").strip()
                if external_url:
                    links.append(
                        f'<a class="agri-nav-link" href="{escape(external_url, quote=True)}" '
                        'target="_blank" rel="noopener noreferrer">'
                        f'{escape(item.label)}</a>'
                    )
                else:
                    links.append(f'<span class="agri-nav-link is-disabled">{escape(item.label)}（暂不可用）</span>')
                continue
            links.append(
                '<a class="agri-nav-link {active}" href="?workspace_page={target}">{label}</a>'.format(
                    active="is-active" if item.target == selected_page else "",
                    target=escape(quote(item.target), quote=True),
                    label=escape(item.label),
                )
            )
        group_markup.append(
            f'<section class="agri-nav-group"><div class="agri-nav-group-title">{escape(group.title)}</div>{"".join(links)}</section>'
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
    researcher_name: str,
    phone: str,
    email: str,
) -> None:
    st.html(
        f'''<header class="agri-home-header">
  <div class="agri-home-kicker">AGRICULTURAL RESEARCH WORKBENCH</div>
  <h1 class="agri-home-title">{escape(title)}</h1>
  <p class="agri-home-meta"><span class="agri-home-researcher">{escape(researcher_name)}</span> · <span class="agri-home-contact">{escape(phone)} · {escape(email)}</span></p>
</header>'''
    )


def render_section_heading(title: str, description: str = "") -> None:
    description_markup = f"<p>{escape(description)}</p>" if description else ""
    st.html(f'<section class="agri-section"><h2>{escape(title)}</h2>{description_markup}</section>')


_DESCRIPTION_KEYWORDS = {
    "spreads_dashboard": ("跨期价差", "跨品种套利"),
    "basis_domestic": ("现货基差", "一口价"),
}

_DETAIL_KEYWORDS = {
    "soybean_weekly": ("种植进度", "生长状况", "出口销售", "出口装船"),
    "soybean_weather": ("美国", "巴西", "阿根廷"),
    "rapeseed_weather": ("加拿大", "澳大利亚", "欧盟", "俄罗斯", "乌克兰"),
    "palm_oil_weather": ("印度尼西亚", "马来西亚"),
    "india_crop_weather": ("棉花", "甘蔗"),
}


def _emphasize_keywords(text: str, keywords: tuple[str, ...]) -> str:
    """Escape text before adding markup for the component's controlled keyword list."""

    markup = escape(text)
    for keyword in keywords:
        escaped_keyword = escape(keyword)
        markup = markup.replace(
            escaped_keyword,
            f'<strong class="agri-card-keyword">{escaped_keyword}</strong>',
        )
    return markup


def render_navigation_card(item: NavigationItem) -> str:
    """Return one static research-navigation card without runtime health state."""

    if not item.external_env:
        action = (
            f'<a class="agri-card-action" href="?workspace_page={escape(quote(item.target), quote=True)}">进入模块</a>'
        )
    elif external_url := os.getenv(item.external_env, "").strip():
        action = (
            f'<a class="agri-card-action" href="{escape(external_url, quote=True)}" '
            'target="_blank" rel="noopener noreferrer">打开独立应用</a>'
        )
    else:
        action = '<span class="agri-card-action is-disabled" aria-disabled="true">暂不可用</span>'

    description_markup = _emphasize_keywords(
        item.description,
        _DESCRIPTION_KEYWORDS.get(item.key, ()),
    )
    detail_markup = _emphasize_keywords(
        item.detail_text or "",
        _DETAIL_KEYWORDS.get(item.key, ()),
    )

    return f'''<article class="agri-card">
  <div class="agri-card-top">{_icon_markup(item.icon)}
    <div class="agri-card-title-group"><h3>{escape(item.label)}</h3></div></div>
  <p class="agri-card-description">{description_markup}</p>
  {f'<p class="agri-card-detail"><span class="agri-card-detail-label">{escape(item.detail_label)}：</span>{detail_markup}</p>' if item.detail_label else '<p class="agri-card-detail"></p>'}
  {action}
</article>'''


def render_home_footer(message: str) -> None:
    st.html(f'<footer class="agri-footer">{escape(message)}</footer>')
