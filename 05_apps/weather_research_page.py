from __future__ import annotations

from collections.abc import Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

import streamlit as st
import streamlit.components.v1 as components

from crop_weather_page import render_weather_page


WeatherCountry = tuple[str, str]


def _render_country_weather(country: str) -> Callable[[], None]:
    return lambda: render_weather_page(country)


WEATHER_COUNTRY_RENDERERS: Mapping[str, Callable[[], None]] = {
    country: _render_country_weather(country)
    for country in ("USA", "BRA", "ARG", "CAN", "AUS", "EU", "RUS", "UKR", "IDN", "MYS", "IND_COTTON", "IND_SUGARCANE")
}

WEATHER_RESEARCH_PAGES: Mapping[str, dict[str, object]] = {
    "soybean_weather": {
        "title": "大豆天气研究",
        "country_options": {"美国": "USA", "巴西": "BRA", "阿根廷": "ARG"},
        "available_countries": frozenset(("USA", "BRA", "ARG")),
    },
    "rapeseed_weather": {
        "title": "菜籽天气研究",
        "country_options": {
            "加拿大": "CAN",
            "澳大利亚": "AUS",
            "欧盟": "EU",
            "俄罗斯": "RUS",
            "乌克兰": "UKR",
        },
        "available_countries": frozenset(("CAN", "AUS", "EU", "RUS", "UKR")),
    },
    "palm_oil_weather": {
        "title": "棕榈油天气研究",
        "country_options": {"印度尼西亚": "IDN", "马来西亚": "MYS"},
        "available_countries": frozenset(("IDN", "MYS")),
    },
    "india_crop_weather": {
        "title": "印度作物天气研究",
        "country_options": {"印度棉花": "IND_COTTON", "印度甘蔗": "IND_SUGARCANE"},
        "available_countries": frozenset(("IND_COTTON", "IND_SUGARCANE")),
    },
}


def _default_country_index(countries: tuple[WeatherCountry, ...], available_countries: frozenset[str]) -> int:
    return next((index for index, (country_key, _label) in enumerate(countries) if country_key in available_countries), 0)


def render_weather_research_page(page_key: str) -> None:
    """Render one weather crop entry; only approved data-backed countries call a page implementation."""

    page = WEATHER_RESEARCH_PAGES[page_key]
    country_options = dict(page["country_options"])
    countries = tuple((country_code, label) for label, country_code in country_options.items())
    available_countries = frozenset(page["available_countries"])
    labels = list(country_options)
    default_index = _default_country_index(countries, available_countries)

    st.title(str(page["title"]))
    selected_label = st.radio(
        "国家/地区",
        labels,
        index=default_index,
        horizontal=True,
        key=f"weather_country_{page_key}",
    )
    selected_country = country_options[selected_label]

    if selected_country in available_countries:
        WEATHER_COUNTRY_RENDERERS[selected_country]()
        return

    st.info("该地区天气研究页面尚未接入。")


def weather_service_url(workspace_url: str, *, embedded: bool = False) -> str:
    """Use the site's host and scheme; never forward queries or credentials.

    The approved Guangxi service uses the standard web port on the same host.
    Loopback development uses the retained local reader on port 8856.
    """
    origin = urlsplit(workspace_url)
    if origin.scheme not in {"http", "https"} or not origin.hostname:
        raise ValueError("当前工作台地址不可用")
    if origin.username or origin.password:
        raise ValueError("工作台地址不能包含身份信息")
    hostname = origin.hostname
    host = f"[{hostname}]" if ":" in hostname else hostname
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        host += ":8856"
    return urlunsplit((origin.scheme, host, "/", "embed=true" if embedded else "", ""))


def render_guangxi_weather_page() -> None:
    """Embed the existing live reader without collecting or copying data."""
    st.title("广西甘蔗天气研究")
    st.caption("14市 · 29个代表点 · 2024年甘蔗面积权重 · IFS历史与2020—2024年均值 · EC/GFS预报")
    try:
        url = weather_service_url(st.context.url)
        embedded_url = weather_service_url(st.context.url, embedded=True)
    except ValueError as exc:
        st.warning(str(exc))
        return
    st.link_button("全屏打开广西天气", url)
    components.iframe(embedded_url, height=1800, scrolling=True)


def yunnan_weather_service_url(workspace_url: str, *, embedded: bool = False) -> str:
    """Use the dedicated Yunnan reader on the same host, without private URL parts."""
    validated = urlsplit(weather_service_url(workspace_url))
    hostname = validated.hostname
    host = f"[{hostname}]" if ":" in hostname else hostname
    return urlunsplit((validated.scheme, host + ":8858", "/", "embed=true" if embedded else "", ""))


def render_yunnan_weather_page() -> None:
    """Read the independently updated Yunnan snapshot; navigation never collects."""
    st.title("云南甘蔗天气研究")
    st.caption("13州市 · 26个蔗区乡镇代表点 · 2024年甘蔗面积权重 · IFS历史与2020—2024年均值 · EC/GFS预报")
    try:
        url = yunnan_weather_service_url(st.context.url)
        embedded_url = yunnan_weather_service_url(st.context.url, embedded=True)
    except ValueError as exc:
        st.warning(str(exc))
        return
    st.link_button("全屏打开云南天气", url)
    components.iframe(embedded_url, height=1800, scrolling=True)
