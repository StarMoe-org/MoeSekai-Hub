from __future__ import annotations

import asyncio
import random
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

RETRYABLE_STATUS_CODES = {429, 468, 500, 502, 503, 504}


@dataclass(frozen=True)
class RetryConfig:
    attempts: int = 5
    backoff_base_seconds: float = 0.5
    backoff_jitter_seconds: float = 0.2


DEFAULT_RETRY_CONFIG = RetryConfig()


def build_headers(extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    return headers


def create_async_client(
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 20.0,
    follow_redirects: bool = True,
    trust_env: bool = False,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=build_headers(headers),
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=follow_redirects,
        trust_env=trust_env,
    )


def _parse_retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _compute_backoff_delay(
    attempt: int,
    retry_config: RetryConfig,
    response: httpx.Response | None = None,
) -> float:
    retry_after_seconds = _parse_retry_after_seconds(response)
    exponential_seconds = retry_config.backoff_base_seconds * (2 ** (attempt - 1))
    jitter_seconds = random.uniform(0, retry_config.backoff_jitter_seconds)
    computed = float(exponential_seconds + jitter_seconds)
    if retry_after_seconds is not None:
        return float(max(retry_after_seconds, computed))
    return float(computed)


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retry_config: RetryConfig = DEFAULT_RETRY_CONFIG,
    retryable_status_codes: Iterable[int] = RETRYABLE_STATUS_CODES,
    **kwargs: Any,
) -> httpx.Response:
    retryable_status_set = set(retryable_status_codes)
    last_error: Exception | None = None

    for attempt in range(1, retry_config.attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code in retryable_status_set and attempt < retry_config.attempts:
                await asyncio.sleep(_compute_backoff_delay(attempt, retry_config, response))
                continue
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            last_error = exc
            status_code = exc.response.status_code
            if status_code not in retryable_status_set or attempt >= retry_config.attempts:
                raise
            await asyncio.sleep(_compute_backoff_delay(attempt, retry_config, exc.response))
        except httpx.TransportError as exc:
            last_error = exc
            if attempt >= retry_config.attempts:
                raise
            await asyncio.sleep(_compute_backoff_delay(attempt, retry_config))

    if last_error is not None:
        raise last_error
    raise RuntimeError("Retry loop exited unexpectedly")


async def get_text(client: httpx.AsyncClient, url: str, **kwargs: Any) -> str:
    response = await request_with_retry(client, "GET", url, **kwargs)
    return response.text


async def get_json(client: httpx.AsyncClient, url: str, **kwargs: Any) -> Any:
    response = await request_with_retry(client, "GET", url, **kwargs)
    return response.json()


async def get_bytes(client: httpx.AsyncClient, url: str, **kwargs: Any) -> bytes:
    response = await request_with_retry(client, "GET", url, **kwargs)
    return response.content
