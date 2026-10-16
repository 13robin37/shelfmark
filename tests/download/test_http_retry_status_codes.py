"""Which HTTP failures html_get_page() keeps retrying and which it gives up on at once."""

import pytest
import requests


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.url = "https://example.org/book/abc"
        self.text = "<html><body>error</body></html>"
        self.cookies: dict[str, str] = {}
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.is_redirect = False

    def raise_for_status(self) -> None:
        error = requests.exceptions.HTTPError(f"{self.status_code} Error")
        error.response = self
        raise error


def _fetch(monkeypatch, status_code: int, retry: int = 5) -> tuple[str, int]:
    import shelfmark.download.http as http

    monkeypatch.setattr(http, "_apply_cf_bypass", lambda _url, _headers: {})
    monkeypatch.setattr(http, "get_proxies", lambda _url: {})
    monkeypatch.setattr(http, "get_ssl_verify", lambda _url: True)
    monkeypatch.setattr(http.network, "should_rotate_dns_for_url", lambda _url: False)
    monkeypatch.setattr(http.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(http, "_is_cf_bypass_enabled", lambda: False)

    calls: list[str] = []
    monkeypatch.setattr(
        http.requests,
        "get",
        lambda url, **_kwargs: calls.append(url) or _FakeResponse(status_code),
    )
    html = http.html_get_page("https://example.org/book/abc", retry=retry, success_delay=0)
    return html, len(calls)


@pytest.mark.parametrize("status_code", [400, 401, 405, 410, 451])
def test_client_errors_are_not_retried(monkeypatch, status_code: int) -> None:
    html, calls = _fetch(monkeypatch, status_code)
    assert html == ""
    assert calls == 1


@pytest.mark.parametrize("status_code", [408, 500, 502, 522, 524])
def test_transient_errors_use_the_whole_retry_budget(monkeypatch, status_code: int) -> None:
    html, calls = _fetch(monkeypatch, status_code)
    assert html == ""
    assert calls == 5
