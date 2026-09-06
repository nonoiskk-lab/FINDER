"""Access-safety behaviour: host allow-list, CAPTCHA, robots, issue recording.

These are compliance guarantees, not conveniences — see docs/COMPLIANCE.md.
"""

from __future__ import annotations

import pytest
import requests

from gem_intel.http_client import (
    AccessBlocked,
    DisallowedHost,
    GemHttpClient,
    RateLimiter,
    RobotsPolicy,
)


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, url="https://x/"):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = headers or {"Content-Type": "text/html"}
        self.url = url

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def iter_content(self, chunk_size=65536):
        yield self.content

    def close(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        item = self.responses.pop(0) if self.responses else FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


@pytest.fixture
def fast_settings(settings):
    """Same settings, but with the politeness delays removed for tests."""
    settings.raw["source"]["min_delay_seconds"] = 0
    settings.raw["source"]["max_delay_seconds"] = 0
    settings.raw["source"]["requests_per_minute"] = 100000
    settings.raw["source"]["max_retries"] = 2
    settings.raw["source"]["backoff_base_seconds"] = 0
    settings.raw["source"]["respect_robots_txt"] = False
    return settings


@pytest.fixture
def client(fast_settings):
    return GemHttpClient(fast_settings, session=FakeSession([]))


@pytest.mark.parametrize("url", [
    "https://tenders.example.com/bids",
    "https://gem.gov.in.attacker.net/bids",
    "http://localhost:8000/bids",
])
def test_non_official_hosts_are_refused(client, url):
    with pytest.raises(DisallowedHost):
        client.assert_allowed_host(url)


@pytest.mark.parametrize("url", [
    "https://bidplus.gem.gov.in/all-bids",
    "https://gem.gov.in/x",
    "https://sub.gem.gov.in/x",
])
def test_official_hosts_are_allowed(client, url):
    client.assert_allowed_host(url)      # must not raise


def test_captcha_aborts_and_is_never_retried(fast_settings):
    session = FakeSession([
        FakeResponse(text="<div class='g-recaptcha'>verify you are a human</div>"),
    ])
    client = GemHttpClient(fast_settings, session=session)
    with pytest.raises(AccessBlocked):
        client.fetch("https://bidplus.gem.gov.in/all-bids", stage="listing")
    assert len(session.calls) == 1               # no retry loop against a challenge
    assert client.access_issues[0].error_type == "CAPTCHA_OR_BOT_CHALLENGE"


def test_server_errors_are_retried_then_recorded(fast_settings):
    session = FakeSession([FakeResponse(status_code=503),
                           FakeResponse(status_code=503)])
    client = GemHttpClient(fast_settings, session=session)
    assert client.fetch("https://bidplus.gem.gov.in/all-bids") is None
    assert len(session.calls) == 2
    assert client.access_issues[0].error_type == "NETWORK_FAILURE"


def test_network_failure_returns_none_rather_than_raising(fast_settings):
    session = FakeSession([requests.ConnectionError("dns"), requests.Timeout("slow")])
    client = GemHttpClient(fast_settings, session=session)
    assert client.fetch("https://bidplus.gem.gov.in/all-bids") is None
    assert client.access_issues


def test_forbidden_is_recorded_not_retried(fast_settings):
    session = FakeSession([FakeResponse(status_code=403, text="Forbidden")])
    client = GemHttpClient(fast_settings, session=session)
    assert client.fetch("https://bidplus.gem.gov.in/all-bids") is None
    assert client.access_issues[0].error_type == "HTTP_403"


def test_oversized_documents_are_skipped(fast_settings):
    session = FakeSession([FakeResponse(text="x" * 5000,
                                        headers={"Content-Type": "application/pdf"})])
    client = GemHttpClient(fast_settings, session=session)
    result = client.fetch("https://bidplus.gem.gov.in/doc.pdf",
                          binary=True, max_bytes=100)
    assert result is None
    assert client.access_issues[0].error_type == "DOCUMENT_TOO_LARGE"


def test_robots_disallow_blocks_the_fetch(fast_settings, monkeypatch):
    fast_settings.raw["source"]["respect_robots_txt"] = True
    client = GemHttpClient(fast_settings, session=FakeSession([]))
    monkeypatch.setattr(client.robots, "allows", lambda url: False)
    assert client.fetch("https://bidplus.gem.gov.in/all-bids") is None
    assert client.access_issues[0].error_type == "ROBOTS_DISALLOWED"


def test_robots_fails_closed_when_unreadable(monkeypatch):
    policy = RobotsPolicy("test-agent", enabled=True)

    def explode(*args, **kwargs):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(requests, "get", explode)
    assert policy.allows("https://bidplus.gem.gov.in/all-bids") is False


def test_missing_robots_txt_means_unrestricted(monkeypatch):
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: FakeResponse(status_code=404, text=""))
    policy = RobotsPolicy("test-agent", enabled=True)
    assert policy.allows("https://bidplus.gem.gov.in/all-bids") is True


def test_rate_limiter_enforces_a_floor_delay():
    import time

    limiter = RateLimiter(per_minute=100000, min_delay=0.05, max_delay=0.05)
    limiter.wait()
    started = time.monotonic()
    limiter.wait()
    assert time.monotonic() - started >= 0.04


def test_captcha_markers_are_detected():
    for body in ["<script src='https://www.google.com/recaptcha/api.js'></script>",
                 "Please verify you are a human",
                 "Incapsula incident ID: 123"]:
        assert GemHttpClient.looks_like_captcha(body)
    assert not GemHttpClient.looks_like_captcha("<div class='card'>GEM/2026/B/1</div>")
