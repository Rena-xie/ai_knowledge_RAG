"""受 SSRF 防护的抓取器（见《项目介绍.md》9.1、10.4）。"""

import httpx
import pytest

from app.core.errors import ErrorCode
from app.infra.fetcher import Fetcher, FetchError

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
