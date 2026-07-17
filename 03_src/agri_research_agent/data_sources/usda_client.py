"""Minimal, secret-safe client for the USDA NASS Quick Stats API."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any


NASS_API_ENDPOINT = "https://quickstats.nass.usda.gov/api/api_GET/"
SOYBEAN_PLANTED_YEARS = tuple(range(2021, 2027))
SOYBEAN_PROGRESS_LEVELS = ("NATIONAL", "STATE")
SOYBEAN_STATISTIC_CATEGORIES = ("PROGRESS", "CONDITION")

SOYBEAN_WEEKLY_QUERY = {
    "source_desc": "SURVEY",
    "sector_desc": "CROPS",
    "group_desc": "FIELD CROPS",
    "commodity_desc": "SOYBEANS",
    "freq_desc": "WEEKLY",
    # Quick Stats does not accept unit_desc=PCT for crop progress. Its real
    # values are stage-specific (PCT PLANTED, PCT GOOD, ...). LIKE=PCT is the
    # query-operator form that returns the complete candidate set to audit.
    "unit_desc__LIKE": "PCT",
    "format": "JSON",
}
SOYBEAN_PROGRESS_QUERY = {
    **SOYBEAN_WEEKLY_QUERY,
    "statisticcat_desc": "PROGRESS",
}

Transport = Callable[[str, int], tuple[int, bytes]]


class NassApiError(RuntimeError):
    """A sanitized USDA NASS request or response failure."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        retry_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.retry_count = retry_count


def redact_secret(value: object, secret: str) -> str:
    """Return text safe for logs, manifests, reports, and exceptions."""

    text = str(value)
    return text.replace(secret, "[REDACTED]") if secret else text


def build_soybean_progress_query(year: int, agg_level_desc: str) -> dict[str, str]:
    """Build one year-by-geography query without including an API key."""

    if year not in SOYBEAN_PLANTED_YEARS:
        raise ValueError(f"Unsupported soybean crop-progress year: {year}")
    level = agg_level_desc.upper()
    if level not in SOYBEAN_PROGRESS_LEVELS:
        raise ValueError(f"Unsupported aggregate level: {agg_level_desc}")
    return {
        **SOYBEAN_PROGRESS_QUERY,
        "agg_level_desc": level,
        "year": str(year),
    }


def build_soybean_progress_query_grid() -> list[dict[str, str]]:
    """Return the required 12 independent year-by-geography requests."""

    return [
        build_soybean_progress_query(year, level)
        for year in SOYBEAN_PLANTED_YEARS
        for level in SOYBEAN_PROGRESS_LEVELS
    ]


def build_soybean_crop_weekly_query(
    year: int,
    agg_level_desc: str,
    statisticcat_desc: str,
) -> dict[str, str]:
    """Build one secret-free year/category/geography crop-weekly query."""

    if year < 1900 or year > 9999:
        raise ValueError(f"Invalid soybean crop-weekly year: {year}")
    level = agg_level_desc.upper()
    if level not in SOYBEAN_PROGRESS_LEVELS:
        raise ValueError(f"Unsupported aggregate level: {agg_level_desc}")
    category = statisticcat_desc.upper()
    if category not in SOYBEAN_STATISTIC_CATEGORIES:
        raise ValueError(f"Unsupported statistic category: {statisticcat_desc}")
    return {
        **SOYBEAN_WEEKLY_QUERY,
        "statisticcat_desc": category,
        "agg_level_desc": level,
        "year": str(year),
    }


def build_soybean_crop_weekly_query_grid() -> list[dict[str, str]]:
    """Return 24 independent year-by-category-by-geography requests."""

    return [
        build_soybean_crop_weekly_query(year, level, category)
        for year in SOYBEAN_PLANTED_YEARS
        for category in SOYBEAN_STATISTIC_CATEGORIES
        for level in SOYBEAN_PROGRESS_LEVELS
    ]


def build_soybean_crop_weekly_current_year_query_grid(
    year: int,
) -> list[dict[str, str]]:
    """Return the four category-by-geography requests for one reporting year."""

    return [
        build_soybean_crop_weekly_query(year, level, category)
        for category in SOYBEAN_STATISTIC_CATEGORIES
        for level in SOYBEAN_PROGRESS_LEVELS
    ]


def _default_transport(url: str, timeout: int) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "commodity-research-system/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status), response.read()


def _safe_error_body(error: urllib.error.HTTPError, api_key: str) -> str:
    try:
        body = error.read(2_000).decode("utf-8", errors="replace")
    except Exception:
        body = ""
    return redact_secret(body, api_key)


def fetch_nass_json(
    query_params: Mapping[str, str],
    *,
    api_key: str,
    timeout: int = 60,
    retries: int = 2,
    transport: Transport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Fetch and validate one JSON response without exposing the API key.

    ``retry_count`` is the number of retries actually performed, excluding the
    first attempt. Only transient network and server errors are retried.
    """

    if not api_key:
        raise NassApiError("NASS_API_KEY is required")
    if retries < 0:
        raise ValueError("retries must be non-negative")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    caller = transport or _default_transport
    params_with_key = {**dict(query_params), "key": api_key}
    url = f"{NASS_API_ENDPOINT}?{urllib.parse.urlencode(params_with_key)}"
    transient_statuses = {429, 500, 502, 503, 504}
    last_error = ""
    last_http_status: int | None = None

    for attempt in range(retries + 1):
        try:
            status, body = caller(url, timeout)
            if status in transient_statuses:
                raise NassApiError(
                    f"USDA NASS transient HTTP {status}",
                    http_status=status,
                    retry_count=attempt,
                )
            if status < 200 or status >= 300:
                raise NassApiError(
                    f"USDA NASS HTTP {status}",
                    http_status=status,
                    retry_count=attempt,
                )
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise NassApiError("USDA NASS returned invalid JSON") from exc
            if not isinstance(payload, dict) or not isinstance(
                payload.get("data"), list
            ):
                raise NassApiError("USDA NASS JSON does not contain a data list")
            fetched_at = (now or (lambda: datetime.now(timezone.utc)))()
            return {
                "http_status": status,
                "payload": payload,
                "record_count": len(payload["data"]),
                "retry_count": attempt,
                "fetched_at_utc": fetched_at.astimezone(timezone.utc).isoformat(),
            }
        except urllib.error.HTTPError as exc:
            body = _safe_error_body(exc, api_key)
            last_http_status = exc.code
            last_error = f"USDA NASS HTTP {exc.code}"
            if body:
                last_error = f"{last_error}: {body}"
            retryable = exc.code in transient_statuses
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_http_status = None
            last_error = redact_secret(
                f"USDA NASS network error: {type(exc).__name__}: {exc}",
                api_key,
            )
            retryable = True
        except NassApiError as exc:
            last_http_status = exc.http_status
            last_error = redact_secret(exc, api_key)
            retryable = any(
                f"HTTP {status}" in last_error for status in transient_statuses
            )

        if not retryable or attempt >= retries:
            raise NassApiError(
                last_error,
                http_status=last_http_status,
                retry_count=attempt,
            ) from None
        sleep(min(2**attempt, 8))

    raise NassApiError(
        last_error or "USDA NASS request failed",
        http_status=last_http_status,
        retry_count=retries,
    )
