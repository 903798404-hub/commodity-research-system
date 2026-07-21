"""Compatibility import for the former soybean-specific weather module.

New crop-neutral callers must import :mod:`agri_research_agent.weather.crop_weather`.
"""

from .crop_weather import *  # noqa: F401,F403
