"""免密钥 HTTP 抓取工具（标准库 urllib + 磁盘缓存）。

红线：零 API Key
- 只做 GET，禁发 Authorization 头，禁读 *_API_KEY / *_TOKEN / *_SECRET 环境变量；
- tests/test_no_api_keys.py 静态扫描守住这条线。

能力
- 超时 / 重试 / 指数退避 + 每主机最小请求间隔（对免密钥公共端点友好）；
- JSON 与文本（arXiv Atom XML）两类响应；
- 磁盘缓存（默认 output/.litcache），离线模式只读缓存，保证可复现；
- set_transport() 注入假传输层，CI / 单测不产生真实网络请求。

所有抓取到的原始响应（正文与落盘缓存）同时是文献真实性核验的证据来源。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from utils.config_loader import PROJECT_ROOT, load_literature_config
from utils.logger import get_logger

logger = get_logger("web-fetch")

Transport = Callable[[str, dict[str, Any], dict[str, str], float], str]

_FORBIDDEN_ENV_SUFFIXES = ("_API_KEY", "_APIKEY", "_ACCESS_TOKEN", "_SECRET", "_SECRET_KEY")
_LAST_CALL: dict[str, float] = {}
_CALL_STATS: dict[str, int] = {"requests": 0, "cache_hits": 0, "failures": 0}
_RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


class NetworkDisabledError(RuntimeError):
    """离线模式（或 network.enabled=false）下缓存未命中。"""


class FetchError(RuntimeError):
    """重试后仍失败的抓取错误。"""


@dataclass
class FetchResult:
    url: str
    params: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    cached: bool = False
    fetched_at: str = ""
    content_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "params": dict(self.params),
            "cached": self.cached,
            "fetched_at": self.fetched_at,
            "content_type": self.content_type,
        }


# --------------------------------------------------------------------------- #
# 零 Key 自检与传输层
# --------------------------------------------------------------------------- #


def forbidden_env_keys(environ: dict[str, str] | None = None) -> list[str]:
    """列出当前环境中形如 API Key 的变量名（仅用于自检告警，绝不读取其值）。"""
    source = os.environ if environ is None else environ
    hits = []
    for name in source:
        upper = name.upper()
        if any(upper.endswith(suffix) for suffix in _FORBIDDEN_ENV_SUFFIXES):
            hits.append(name)
    return sorted(hits)


def _assert_key_free(headers: dict[str, str]) -> None:
    for name in headers:
        if name.lower() in ("authorization", "x-api-key", "api-key", "x-subscription-token"):
            raise AssertionError(f"零 API Key 红线：禁止发送 {name} 请求头")
    for name in headers:
        if "key" in name.lower() or "token" in name.lower():
            value = str(headers[name])
            if value.strip():
                raise AssertionError(f"零 API Key 红线：请求头 {name} 携带疑似密钥值")


def _urllib_transport(url: str, params: dict[str, Any], headers: dict[str, str], timeout: float) -> str:
    _assert_key_free(headers)
    query = urllib.parse.urlencode(params, doseq=True)
    target = f"{url}?{query}" if query else url
    request = urllib.request.Request(target, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 仅访问配置白名单端点
        payload = response.read()
    return payload.decode("utf-8", errors="replace")


_TRANSPORT: Transport = _urllib_transport


def set_transport(transport: Transport | None) -> None:
    """注入自定义传输层（测试用）；传 None 恢复 urllib 实现。"""
    global _TRANSPORT
    _TRANSPORT = transport or _urllib_transport


def reset_stats() -> None:
    _CALL_STATS.update({"requests": 0, "cache_hits": 0, "failures": 0})
    _LAST_CALL.clear()


def stats() -> dict[str, int]:
    return dict(_CALL_STATS)


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #


def _cache_dir(config: dict[str, Any]) -> Path:
    raw = str((config.get("network") or {}).get("cache_dir") or "output/.litcache")
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _cache_key(url: str, params: dict[str, Any]) -> str:
    canonical = url + "?" + urllib.parse.urlencode(sorted((params or {}).items()), doseq=True)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def _read_cache(path: Path, ttl_days: int) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = str(payload.get("fetched_at") or "")
        stamp = datetime.fromisoformat(fetched_at) if fetched_at else None
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if stamp is not None and ttl_days >= 0:
        if datetime.now(timezone.utc) - stamp > timedelta(days=ttl_days):
            return None
    return payload


def _write_cache(path: Path, url: str, params: dict[str, Any], body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {"url": url, "params": params, "fetched_at": stamp, "body": body}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return stamp


def cache_stats(config: dict[str, Any] | None = None) -> dict[str, Any]:
    folder = _cache_dir(config or load_literature_config())
    files = sorted(folder.glob("*.json")) if folder.exists() else []
    return {"dir": str(folder), "entries": len(files)}


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #


def _respect_interval(host: str, min_interval: float) -> None:
    if min_interval <= 0:
        return
    last = _LAST_CALL.get(host)
    if last is not None:
        elapsed = time.monotonic() - last
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
    _LAST_CALL[host] = time.monotonic()


def _retry_after_seconds(error: urllib.error.HTTPError) -> float:
    """读取 Retry-After（秒或 HTTP 日期），无法解析时返回 0。"""
    raw = str((error.headers or {}).get("Retry-After") or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(raw)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0.0


def fetch_text(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    config: dict[str, Any] | None = None,
    min_interval: float | None = None,
    offline: bool | None = None,
    force_refresh: bool = False,
    accept: str = "application/json",
) -> FetchResult:
    """GET 抓取文本响应，命中缓存直接返回。"""
    config = config or load_literature_config()
    network = config.get("network") or {}
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    cache_path = _cache_dir(config) / f"{_cache_key(url, params)}.json"
    ttl_days = int(network.get("cache_ttl_days", 30))

    if not force_refresh:
        cached = _read_cache(cache_path, ttl_days)
        if cached is not None:
            _CALL_STATS["cache_hits"] += 1
            return FetchResult(
                url=cached.get("url", url),
                params=cached.get("params", params),
                body=str(cached.get("body", "")),
                cached=True,
                fetched_at=str(cached.get("fetched_at", "")),
                content_type="cached",
            )

    disabled = offline if offline is not None else bool(network.get("offline"))
    if disabled or not bool(network.get("enabled", True)):
        raise NetworkDisabledError(f"离线模式且缓存未命中: {url}")

    headers = {
        "User-Agent": str(network.get("user_agent") or "paper-polish-project/1.0"),
        "Accept": accept,
    }
    timeout = float(network.get("timeout_seconds", 20))
    attempts = int(network.get("max_retries", 2)) + 1
    backoff = float(network.get("backoff_seconds", 1.5))
    interval = float(min_interval if min_interval is not None else network.get("min_interval_seconds", 1.0))
    host = urllib.parse.urlsplit(url).netloc or url

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            _respect_interval(host, interval)
            body = _TRANSPORT(url, params, headers, timeout)
            _CALL_STATS["requests"] += 1
            stamp = _write_cache(cache_path, url, params, body)
            return FetchResult(url=url, params=params, body=body, cached=False, fetched_at=stamp, content_type=accept)
        except AssertionError:
            raise
        except urllib.error.HTTPError as exc:
            last_error = exc
            _CALL_STATS["failures"] += 1
            logger.warning(
                "抓取失败（第 %d/%d 次）url=%s err=HTTP %s",
                attempt + 1,
                attempts,
                url,
                exc.code,
            )
            if exc.code not in _RETRYABLE_HTTP_STATUS or attempt + 1 >= attempts:
                exc.close()
                raise FetchError(f"抓取失败: {url} (HTTP {exc.code})") from exc
            delay = max(backoff * (2**attempt), _retry_after_seconds(exc))
            exc.close()
            time.sleep(min(delay, 60.0))
        except Exception as exc:  # noqa: BLE001 - 统一转成 FetchError 并保留原因
            last_error = exc
            _CALL_STATS["failures"] += 1
            logger.warning("抓取失败（第 %d/%d 次）url=%s err=%s", attempt + 1, attempts, url, exc)
            if attempt + 1 < attempts:
                time.sleep(backoff * (2**attempt))
    raise FetchError(f"抓取失败: {url} ({last_error})")


def fetch_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    config: dict[str, Any] | None = None,
    min_interval: float | None = None,
    offline: bool | None = None,
    force_refresh: bool = False,
) -> tuple[Any, FetchResult]:
    """GET 抓取 JSON；解析失败抛 FetchError。"""
    result = fetch_text(
        url,
        params,
        config=config,
        min_interval=min_interval,
        offline=offline,
        force_refresh=force_refresh,
        accept="application/json",
    )
    try:
        return json.loads(result.body or "{}"), result
    except json.JSONDecodeError as exc:
        raise FetchError(f"响应不是合法 JSON: {url} ({exc})") from exc
