"""URL 规范化（见《项目开发说明_v1.md》5.3）。"""

import pytest

from app.domain.url import InvalidUrlError, normalize_url, redact_url_credentials


def test_lowercases_scheme_and_host_and_normalizes_empty_path() -> None:
    assert normalize_url("HTTPS://Example.COM") == "https://example.com/"


def test_removes_default_ports() -> None:
    assert normalize_url("http://example.com:80/a") == "http://example.com/a"
    assert normalize_url("https://example.com:443/a") == "https://example.com/a"


def test_keeps_non_default_port() -> None:
    assert normalize_url("https://example.com:8443/a") == "https://example.com:8443/a"


def test_drops_fragment() -> None:
    assert normalize_url("https://example.com/a#section") == "https://example.com/a"


def test_drops_tracking_params_and_keeps_the_rest_in_order() -> None:
    raw = "https://example.com/a?utm_source=x&id=7&gclid=1&fbclid=2&b=2"

    assert normalize_url(raw) == "https://example.com/a?id=7&b=2"


def test_keeps_blank_query_values() -> None:
    assert normalize_url("https://example.com/a?q=") == "https://example.com/a?q="


def test_rejects_non_http_scheme() -> None:
    with pytest.raises(InvalidUrlError):
        normalize_url("ftp://example.com/a")


def test_rejects_missing_host() -> None:
    with pytest.raises(InvalidUrlError):
        normalize_url("https:///a")


def test_rejects_credentials() -> None:
    with pytest.raises(InvalidUrlError) as exc_info:
        normalize_url("https://user:pass@example.com/a")

    assert "user" not in str(exc_info.value)
    assert "pass" not in str(exc_info.value)


def test_rejects_malformed_url() -> None:
    with pytest.raises(InvalidUrlError, match="格式不合法"):
        normalize_url("http://[::1")


def test_redacts_url_credentials() -> None:
    assert (
        redact_url_credentials("https://user:pass@example.com/a?q=1")
        == "https://example.com/a?q=1"
    )


def test_rejects_empty_input() -> None:
    with pytest.raises(InvalidUrlError):
        normalize_url("   ")
