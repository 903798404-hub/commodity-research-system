from __future__ import annotations

from collections.abc import Callable, Mapping

import streamlit as st

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
