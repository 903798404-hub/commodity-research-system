from __future__ import annotations

from collections.abc import Mapping

import streamlit as st

from soybean_weather_page import render_soybean_weather_page


WeatherCountry = tuple[str, str]

WEATHER_RESEARCH_PAGES: Mapping[str, dict[str, object]] = {
    "soybean_weather": {
        "title": "大豆天气研究",
        "countries": (("USA", "美国"), ("BRA", "巴西"), ("ARG", "阿根廷")),
        "available_countries": frozenset({"USA"}),
    },
    "rapeseed_weather": {
        "title": "菜籽天气研究",
        "countries": (("CAN", "加拿大"), ("AUS", "澳大利亚"), ("EU", "欧盟"), ("RUS", "俄罗斯"), ("UKR", "乌克兰")),
        "available_countries": frozenset(),
    },
    "palm_oil_weather": {
        "title": "棕榈油天气研究",
        "countries": (("IDN", "印度尼西亚"), ("MYS", "马来西亚")),
        "available_countries": frozenset(),
    },
    "india_crop_weather": {
        "title": "印度作物天气研究",
        "countries": (("IND_COTTON", "印度棉花"), ("IND_SUGARCANE", "印度甘蔗")),
        "available_countries": frozenset(),
    },
}


def _default_country_index(countries: tuple[WeatherCountry, ...], available_countries: frozenset[str]) -> int:
    return next((index for index, (country_key, _label) in enumerate(countries) if country_key in available_countries), 0)


def render_weather_research_page(page_key: str) -> None:
    """Render one weather crop entry; only approved data-backed countries call a page implementation."""

    page = WEATHER_RESEARCH_PAGES[page_key]
    countries = tuple(page["countries"])
    available_countries = frozenset(page["available_countries"])
    labels = [label for _country_key, label in countries]
    default_index = _default_country_index(countries, available_countries)

    st.title(str(page["title"]))
    selected_label = st.radio(
        "国家/地区",
        labels,
        index=default_index,
        horizontal=True,
        key=f"weather_country_{page_key}",
    )
    selected_country = next(country_key for country_key, label in countries if label == selected_label)

    if page_key == "soybean_weather" and selected_country == "USA":
        render_soybean_weather_page()
        return

    st.info("该地区天气研究页面尚未接入。")
