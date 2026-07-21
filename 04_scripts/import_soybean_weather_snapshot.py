"""Compatibility entry point for :mod:`import_crop_weather_snapshot`."""

from __future__ import annotations

import sys

import import_crop_weather_snapshot as _canonical

if __name__ == "__main__":
    raise SystemExit(_canonical.main())

sys.modules[__name__] = _canonical
