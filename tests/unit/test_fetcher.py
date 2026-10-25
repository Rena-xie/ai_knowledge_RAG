"""受 SSRF 防护的抓取器（见《项目开发说明_v1.md》9.1、10.4）。"""

import httpx
import pytest

from app.core.errors import ErrorCode
from app.infra.fetcher import Fetcher, FetchError
from app.infra.transport import PINNED_IP_EXTENSION

PUBLIC_URL = "https://93.184.216.34/page"


def make_fetcher(handler, **overrides) -> Fetcher:
    options = {
        "timeout_s": 1,
        "max_bytes": 1000,
        "max_redirects": 2,
        "max_retries": 0,
        "backoff_s": 0.0,
    }
    options.update(overrides)
    return Fetcher(transport=httpx.MockTransport(handler), **options)


def test_rejects_non_http_scheme() -> None:
    fetcher = make_fetcher(lambda request: httpx.Response(200, text="x"))

    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch("ftp://93.184.216.34/page")

    assert excinfo.value.code == ErrorCode.SSRF_BLOCKED


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/page",
        "http://10.0.0.1/page",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/page",
    ],
)
def test_rejects_private_and_reserved_addresses(url: str) -> None:
    fetcher = make_fetcher(lambda request: httpx.Response(200, text="x"))

    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch(url)

    assert excinfo.value.code == ErrorCode.SSRF_BLOCKED


def test_fetches_html_page() -> None:
    fetcher = make_fetcher(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, text="<p>hi</p>"
        )
    )

    result = fetcher.fetch(PUBLIC_URL)

    assert result.html == "<p>hi</p>"
    assert result.url == PUBLIC_URL


def test_rejects_unsupported_content_type() -> None:
    fetcher = make_fetcher(
        lambda request: httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF"
        )
    )

    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch(PUBLIC_URL)

    assert excinfo.value.code == ErrorCode.FETCH_UNSUPPORTED_CONTENT


def test_rejects_oversized_body() -> None:
    fetcher = make_fetcher(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"x" * 2000
        )
    )

    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch(PUBLIC_URL)

    assert excinfo.value.code == ErrorCode.FETCH_TOO_LARGE


def test_reports_http_error_status() -> None:
    fetcher = make_fetcher(lambda request: httpx.Response(404))

    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch(PUBLIC_URL)

    assert excinfo.value.code == ErrorCode.FETCH_HTTP_ERROR


def test_revalidates_redirect_target() -> None:
    fetcher = make_fetcher(
        lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/"})
    )

    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch(PUBLIC_URL)

    assert excinfo.value.code == ErrorCode.SSRF_BLOCKED


def test_follows_redirect_to_public_target() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/page":
            return httpx.Response(
                302, headers={"location": "https://93.184.216.34/final"}
            )
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>final</p>"
        )

    result = make_fetcher(handler).fetch(PUBLIC_URL)

    assert result.url == "https://93.184.216.34/final"
    assert result.html == "<p>final</p>"
    assert result.redirect_urls == (PUBLIC_URL,)


def test_rejects_too_many_redirects() -> None:
    fetcher = make_fetcher(
        lambda request: httpx.Response(302, headers={"location": PUBLIC_URL})
    )

    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch(PUBLIC_URL)

    assert excinfo.value.code == ErrorCode.FETCH_HTTP_ERROR


def test_timeout_is_retried_then_reported() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(str(request.url))
        raise httpx.ConnectTimeout("boom")

    fetcher = make_fetcher(handler, max_retries=1)

    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch(PUBLIC_URL)

    assert excinfo.value.code == ErrorCode.FETCH_TIMEOUT
    assert len(calls) == 2


def test_server_error_is_retried_then_succeeds() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>ok</p>"
        )

    result = make_fetcher(handler, max_retries=1).fetch(PUBLIC_URL)

    assert result.html == "<p>ok</p>"
    assert len(calls) == 2


def test_prefers_ipv4_when_host_resolves_to_multiple_addresses() -> None:
    captured: list[object] = []

    def resolver(host: str) -> list[str]:
        return ["2606:4700:4700::1111", "93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        captured.append(request.extensions.get(PINNED_IP_EXTENSION))
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>ok</p>"
        )

    result = make_fetcher(handler, resolver=resolver).fetch(
        "https://example.test/page"
    )

    assert result.html == "<p>ok</p>"
    assert captured == ["93.184.216.34"]


def test_tries_next_address_when_first_one_fails() -> None:
    attempts: list[object] = []

    def resolver(host: str) -> list[str]:
        return ["93.184.216.34", "93.184.216.35"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        address = request.extensions.get(PINNED_IP_EXTENSION)
        attempts.append(address)
        if address == "93.184.216.34":
            raise httpx.ConnectError("连接失败")
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>ok</p>"
        )

    result = make_fetcher(handler, resolver=resolver).fetch(
        "https://example.test/page"
    )

    assert result.html == "<p>ok</p>"
    assert attempts == ["93.184.216.34", "93.184.216.35"]


def test_pins_every_hop_to_validated_ip() -> None:
    captured: list[dict[str, object]] = []

    def resolver(host: str) -> list[str]:
        return {
            "example.test": ["93.184.216.34"],
            "other.test": ["93.184.216.35"],
        }[host]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        captured.append(
            {
                "url": str(request.url),
                "pinned": request.extensions.get(PINNED_IP_EXTENSION),
            }
        )
        if request.url.host == "example.test":
            return httpx.Response(
                302, headers={"location": "https://other.test/final"}
            )
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>ok</p>"
        )

    fetcher = Fetcher(
        timeout_s=1,
        max_bytes=1000,
        max_redirects=2,
        max_retries=0,
        backoff_s=0.0,
        transport=httpx.MockTransport(handler),
        resolver=resolver,
    )

    result = fetcher.fetch("https://example.test/page")

    assert result.url == "https://other.test/final"
    assert [item["pinned"] for item in captured] == [
        "93.184.216.34",
        "93.184.216.35",
    ]
    assert [item["url"] for item in captured] == [
        "https://example.test/page",
        "https://other.test/final",
    ]


def test_robots_disallow_blocks_page_request() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /private",
                headers={"content-type": "text/plain"},
            )
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>ok</p>"
        )

    with pytest.raises(FetchError) as excinfo:
        make_fetcher(handler).fetch("https://93.184.216.34/private/page")

    assert excinfo.value.code == ErrorCode.ROBOTS_DISALLOWED
    assert requested == ["/robots.txt"]


def test_missing_robots_file_allows_fetch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>ok</p>"
        )

    result = make_fetcher(handler).fetch(PUBLIC_URL)

    assert result.html == "<p>ok</p>"


def test_robots_result_is_cached_per_host() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /blocked",
                headers={"content-type": "text/plain"},
            )
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>ok</p>"
        )

    fetcher = make_fetcher(handler)
    fetcher.fetch(PUBLIC_URL)
    fetcher.fetch(PUBLIC_URL)

    assert paths.count("/robots.txt") == 1
    assert paths.count("/page") == 2


def test_sends_configured_user_agent() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent"))
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>ok</p>"
        )

    make_fetcher(handler, user_agent="my-agent/v1").fetch(PUBLIC_URL)

    assert seen == ["my-agent/v1", "my-agent/v1"]


def test_polite_delay_applies_between_requests_to_same_host() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>ok</p>"
        )

    fetcher = make_fetcher(handler, polite_delay_s=1.0, sleeper=sleeps.append)
    fetcher.fetch(PUBLIC_URL)
    fetcher.fetch(PUBLIC_URL)

    assert len(sleeps) == 2
    assert all(0.9 < wait <= 1.0 for wait in sleeps)
