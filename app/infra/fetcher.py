"""受 SSRF 防护的网页抓取器（见《项目开发说明_v1.md》9.1、10.4）。

请求前解析并校验全部 A/AAAA 记录，选定 IP 通过 `pinned_ip` 扩展交给
PinnedTransport 建连（不再二次解析），并对每次重定向重新校验。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.errors import ErrorCode
from app.core.settings import DEFAULT_FETCH_USER_AGENT, DEFAULT_ROBOTS_CACHE_TTL_S
from app.infra.robots import RobotsPolicy
from app.infra.transport import PINNED_IP_EXTENSION, PinnedTransport

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
RETRYABLE_CODES = frozenset({ErrorCode.FETCH_TIMEOUT, ErrorCode.FETCH_HTTP_ERROR})
ROBOTS_MAX_BYTES = 65536


class FetchError(Exception):
    """抓取失败，携带统一错误码。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FetchResult:
    """抓取结果。"""

    url: str
    content_type: str
    html: str
    redirect_urls: tuple[str, ...] = ()


def resolve_host(host: str) -> list[str]:
    """解析主机的全部地址。"""

    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def order_addresses(addresses: Sequence[str]) -> list[str]:
    """按 IPv4 优先的顺序排列候选地址，保证同一主机每次尝试顺序稳定。"""

    def sort_key(address: str) -> tuple[int, int, str]:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return (2, 0, address)
        return (0 if parsed.version == 4 else 1, int(parsed), address)

    return sorted(addresses, key=sort_key)


def is_public_address(address: str) -> bool:
    """判断地址是否属于可访问的公网地址。"""

    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    is_site_local = isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local
    return ip.is_global and not ip.is_reserved and not is_site_local


class Fetcher:
    """带重试、体积限制与内容类型校验的抓取器。"""

    def __init__(
        self,
        *,
        timeout_s: float,
        max_bytes: int,
        max_redirects: int,
        max_retries: int,
        backoff_s: float = 1.0,
        user_agent: str = DEFAULT_FETCH_USER_AGENT,
        polite_delay_s: float = 0.0,
        robots_cache_ttl_s: float = DEFAULT_ROBOTS_CACHE_TTL_S,
        sleeper: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
        resolver: Callable[[str], list[str]] = resolve_host,
        validator: Callable[[str], bool] = is_public_address,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._user_agent = user_agent
        self._polite_delay_s = polite_delay_s
        self._robots_cache_ttl_s = robots_cache_ttl_s
        self._sleeper = sleeper
        self._resolver = resolver
        self._validator = validator
        self._last_request_at: dict[str, float] = {}
        self._robots_cache: dict[str, tuple[float, RobotsPolicy | None]] = {}
        self._client = httpx.Client(
            timeout=timeout_s,
            transport=transport if transport is not None else PinnedTransport(),
            follow_redirects=False,
            headers={"User-Agent": user_agent},
        )

    def close(self) -> None:
        self._client.close()

    def fetch(self, url: str) -> FetchResult:
        last_error: FetchError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._fetch_once(url)
            except FetchError as error:
                if error.code not in RETRYABLE_CODES or attempt >= self._max_retries:
                    raise
                last_error = error
                if self._backoff_s:
                    time.sleep(self._backoff_s * (2**attempt))
        raise last_error or FetchError(ErrorCode.FETCH_HTTP_ERROR, "抓取失败")

    def _validate(self, url: str) -> list[str]:
        """校验协议与全部解析地址，返回按优先级排序的候选地址。"""

        parts = urlsplit(url)
        if parts.scheme not in ALLOWED_SCHEMES:
            raise FetchError(ErrorCode.SSRF_BLOCKED, f"仅支持 HTTP/HTTPS: {url}")
        if parts.username or parts.password:
            raise FetchError(ErrorCode.SSRF_BLOCKED, "URL 不允许携带凭据")
        host = parts.hostname
        if not host:
            raise FetchError(ErrorCode.SSRF_BLOCKED, f"URL 缺少主机名: {url}")
        try:
            addresses = self._resolver(host)
        except OSError as error:
            message = f"域名解析失败: {host}"
            raise FetchError(ErrorCode.FETCH_HTTP_ERROR, message) from error
        if not addresses:
            raise FetchError(ErrorCode.FETCH_HTTP_ERROR, f"域名无解析结果: {host}")
        for address in addresses:
            if not self._validator(address):
                raise FetchError(
                    ErrorCode.SSRF_BLOCKED, f"目标地址被安全策略拦截: {address}"
                )
        return order_addresses(addresses)

    def _fetch_once(self, url: str) -> FetchResult:
        current = url
        redirect_urls: list[str] = []
        for hop in range(self._max_redirects + 1):
            host = urlsplit(current).netloc.lower()
            self._ensure_robots_allowed(current, host)
            candidates = self._validate(current)
            self._respect_polite_delay(host)
            redirect_to: str | None = None
            last_error: FetchError | None = None
            for address in candidates:
                try:
                    with self._client.stream(
                        "GET", current, extensions={PINNED_IP_EXTENSION: address}
                    ) as response:
                        if response.status_code in REDIRECT_STATUSES:
                            redirect_to = self._redirect_target(response, hop)
                            break
                        return self._read_response(
                            response, redirect_urls=tuple(redirect_urls)
                        )
                except httpx.TimeoutException:
                    last_error = FetchError(
                        ErrorCode.FETCH_TIMEOUT, f"抓取超时: {current}"
                    )
                except httpx.TransportError:
                    last_error = FetchError(
                        ErrorCode.FETCH_HTTP_ERROR, f"抓取失败: {current}"
                    )
                except httpx.HTTPError as error:
                    raise FetchError(
                        ErrorCode.FETCH_HTTP_ERROR, f"抓取失败: {current}"
                    ) from error
            if redirect_to is not None:
                redirect_urls.append(current)
                current = redirect_to
                continue
            raise last_error or FetchError(ErrorCode.FETCH_HTTP_ERROR, "抓取失败")
        raise FetchError(ErrorCode.FETCH_HTTP_ERROR, "重定向次数超过上限")

    def _ensure_robots_allowed(self, url: str, host: str) -> None:
        """按 robots.txt 判定当前路径是否允许抓取。"""

        policy = self._robots_policy(url, host)
        if policy is None:
            return
        path = urlsplit(url).path or "/"
        if not policy.allows(path):
            raise FetchError(
                ErrorCode.ROBOTS_DISALLOWED, f"robots.txt 禁止抓取: {url}"
            )

    def _robots_policy(self, url: str, host: str) -> RobotsPolicy | None:
        """返回该主机的 robots 策略；取不到时返回 None（按允许处理）。"""

        now = time.monotonic()
        cached = self._robots_cache.get(host)
        if cached is not None and cached[0] > now:
            return cached[1]
        text = self._fetch_robots(url, host)
        if text is None:
            logger.info(
                "robots.txt 不可用，按允许处理: %s",
                host,
                extra={"stage": "robots"},
            )
        policy = None if text is None else RobotsPolicy.parse(text, self._user_agent)
        expiry = (
            now + self._robots_cache_ttl_s if self._robots_cache_ttl_s > 0 else now
        )
        self._robots_cache[host] = (expiry, policy)
        return policy

    def _fetch_robots(self, url: str, host: str) -> str | None:
        """取该主机的 robots.txt；与页面抓取共用同一套 SSRF 校验与固定 IP 建连。"""

        parts = urlsplit(url)
        robots_url = urlunsplit((parts.scheme, host, "/robots.txt", "", ""))
        try:
            candidates = self._validate(robots_url)
        except FetchError:
            return None
        self._respect_polite_delay(host)
        for address in candidates:
            try:
                with self._client.stream(
                    "GET", robots_url, extensions={PINNED_IP_EXTENSION: address}
                ) as response:
                    if response.status_code >= 400:
                        return None
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > ROBOTS_MAX_BYTES:
                            return None
                    return body.decode(response.encoding or "utf-8", errors="replace")
            except httpx.HTTPError:
                continue
        return None

    def _respect_polite_delay(self, host: str) -> None:
        """保证同一主机的相邻请求间隔不小于 POLITE_DELAY_S。"""

        if self._polite_delay_s <= 0:
            return
        now = time.monotonic()
        last = self._last_request_at.get(host)
        if last is not None:
            wait = self._polite_delay_s - (now - last)
            if wait > 0:
                self._sleeper(wait)
        self._last_request_at[host] = time.monotonic()

    def _redirect_target(self, response: httpx.Response, hop: int) -> str:
        location = response.headers.get("location")
        if not location:
            raise FetchError(ErrorCode.FETCH_HTTP_ERROR, "重定向缺少 Location 头")
        if hop >= self._max_redirects:
            raise FetchError(ErrorCode.FETCH_HTTP_ERROR, "重定向次数超过上限")
        return str(response.url.join(location))

    def _read_response(
        self,
        response: httpx.Response,
        *,
        redirect_urls: tuple[str, ...] = (),
    ) -> FetchResult:
        if response.status_code >= 400:
            raise FetchError(
                ErrorCode.FETCH_HTTP_ERROR,
                f"目标返回 HTTP {response.status_code}",
            )
        content_type = (
            response.headers.get("content-type", "").split(";")[0].strip().lower()
        )
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise FetchError(
                ErrorCode.FETCH_UNSUPPORTED_CONTENT,
                f"不支持的内容类型: {content_type}",
            )
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > self._max_bytes:
                raise FetchError(ErrorCode.FETCH_TOO_LARGE, "响应体超过体积上限")
        return FetchResult(
            url=str(response.url),
            content_type=content_type,
            html=body.decode(response.encoding or "utf-8", errors="replace"),
            redirect_urls=redirect_urls,
        )
