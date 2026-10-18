"""按已校验 IP 建连的 HTTP 传输层（见《项目开发说明_v1.md》9.1）。

Fetcher 在发起请求前解析并校验目标地址，通过请求扩展 `pinned_ip` 传入
选定 IP；本传输层直接使用该 IP 建连（不再二次解析），同时保留原始 Host
头与 TLS SNI，从而消除校验与建连之间的 DNS 重绑定窗口。
"""

from __future__ import annotations

import httpx

PINNED_IP_EXTENSION = "pinned_ip"
SNI_EXTENSION = "sni_hostname"


class PinnedTransport(httpx.BaseTransport):
    """把 TCP 连接固定到已校验 IP，同时保留逻辑主机名。"""

    def __init__(self, *, inner: httpx.BaseTransport | None = None) -> None:
        self._inner = inner if inner is not None else httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        pinned_ip = request.extensions.get(PINNED_IP_EXTENSION)
        if not pinned_ip:
            return self._inner.handle_request(request)

        headers = httpx.Headers(request.headers)
        headers["Host"] = request.url.netloc.decode("ascii")
        pinned_request = httpx.Request(
            method=request.method,
            url=request.url.copy_with(host=str(pinned_ip)),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, SNI_EXTENSION: request.url.host},
        )
        return self._inner.handle_request(pinned_request)

    def close(self) -> None:
        self._inner.close()
