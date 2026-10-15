"""受 SSRF 防护的网页抓取器（见《项目介绍.md》9.1、10.4）。

请求前解析并校验全部 A/AAAA 记录，选定 IP 通过 `pinned_ip` 扩展交给
PinnedTransport 建连（不再二次解析），并对每次重定向重新校验。
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.core.errors import ErrorCode
from app.infra.transport import PINNED_IP_EXTENSION, PinnedTransport

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
RETRYABLE_CODES = frozenset({ErrorCode.FETCH_TIMEOUT, ErrorCode.FETCH_HTTP_ERROR})


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


def resolve_host(host: str) -> list[str]:
    """解析主机的全部地址。"""

    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


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
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


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
        transport: httpx.BaseTransport | None = None,
        resolver: Callable[[str], list[str]] = resolve_host,
        validator: Callable[[str], bool] = is_public_address,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._resolver = resolver
        self._validator = validator
        self._client = httpx.Client(
            timeout=timeout_s,
            transport=transport if transport is not None else PinnedTransport(),
            follow_redirects=False,
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
        for hop in range(self._max_redirects + 1):
            candidates = self._validate(current)
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
                        return self._read_response(response)
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
                current = redirect_to
                continue
            raise last_error or FetchError(ErrorCode.FETCH_HTTP_ERROR, "抓取失败")
        raise FetchError(ErrorCode.FETCH_HTTP_ERROR, "重定向次数超过上限")

    def _redirect_target(self, response: httpx.Response, hop: int) -> str:
        location = response.headers.get("location")
        if not location:
            raise FetchError(ErrorCode.FETCH_HTTP_ERROR, "重定向缺少 Location 头")
        if hop >= self._max_redirects:
            raise FetchError(ErrorCode.FETCH_HTTP_ERROR, "重定向次数超过上限")
        return str(response.url.join(location))

    def _read_response(self, response: httpx.Response) -> FetchResult:
        if response.status_code >= 400:
            raise FetchError(
                ErrorCode.FETCH_HTTP_ERROR,
                f"目标返回 HTTP {response.status_code}",
            )
        content_type = (
            response.headers.get("content-type", "").split(";")[0].strip().lower()
        )
        if content_type and content_type not in ALLOWED_CONTENT_TYPES:
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
        )
