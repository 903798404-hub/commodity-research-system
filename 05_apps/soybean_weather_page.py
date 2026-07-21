"""Compatibility page module for the former soybean-specific import path."""

from __future__ import annotations

import sys

import crop_weather_page as _canonical

sys.modules[__name__] = _canonical
