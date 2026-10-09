"""按已校验 IP 建连的传输层（见《项目介绍.md》9.1）。"""

import httpx

from app.infra.transport import PINNED_IP_EXTENSION, PinnedTransport


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=PinnedTransport(inner=httpx.MockTransport(handler)))


def test_pins_connection_to_validated_ip() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["host"] = request.headers.get("host")
        captured["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, text="ok")

    with make_client(handler) as client:
        response = client.get(
            "https://example.com/page",
            extensions={PINNED_IP_EXTENSION: "93.184.216.34"},
        )

    assert captured["url"] == "https://93.184.216.34/page"
    assert captured["host"] == "example.com"
    assert captured["sni"] == "example.com"
    assert str(response.url) == "https://example.com/page"


def test_keeps_non_default_port_in_host_header() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["host"] = request.headers.get("host")
        return httpx.Response(200, text="ok")

    with make_client(handler) as client:
        client.get(
            "https://example.com:8443/page",
            extensions={PINNED_IP_EXTENSION: "93.184.216.34"},
        )

    assert captured["url"] == "https://93.184.216.34:8443/page"
    assert captured["host"] == "example.com:8443"


def test_without_pinned_ip_passes_request_through() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, text="ok")

    with make_client(handler) as client:
        response = client.get("https://example.com/page")

    assert captured["url"] == "https://example.com/page"
    assert str(response.url) == "https://example.com/page"
