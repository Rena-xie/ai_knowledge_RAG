"""URL 规范化（见《项目介绍.md》5.3）。"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}
TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "fbclid",
        "dclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
    }
)


class InvalidUrlError(ValueError):
    """URL 为空、协议不支持或包含危险成分。"""


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def normalize_url(raw: str) -> str:
    """返回用于鉴重的规范化 URL。

    规则：小写协议与主机、去默认端口、去凭据、去片段、去追踪参数，
    其余查询参数保持原有顺序；空路径规范化为 `/`。
    """

    candidate = (raw or "").strip()
    if not candidate:
        raise InvalidUrlError("URL 为空")

    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidUrlError(f"仅支持 HTTP/HTTPS 协议: {raw}")
    if parts.username or parts.password:
        raise InvalidUrlError(f"URL 不允许携带凭据: {raw}")

    host = parts.hostname
    if not host:
        raise InvalidUrlError(f"URL 缺少主机名: {raw}")
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidUrlError(f"URL 端口不合法: {raw}") from exc

    netloc = host.lower()
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"

    path = parts.path or "/"
    query_pairs = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(name)
    ]
    return urlunsplit((scheme, netloc, path, urlencode(query_pairs), ""))
