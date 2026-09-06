"""Polite, compliant HTTP access to the GeM portal.

Everything about this module is defensive on purpose:

* ``robots.txt`` is fetched once and honoured for every path.
* Requests are rate-limited and jittered; the configured delay is a floor.
* Only hosts on the allow-list can be requested at all.
* A CAPTCHA / bot-interstitial aborts the source. It is never solved,
  never bypassed, and never retried in a loop.
* Every failure is surfaced as an :class:`AccessIssue` so the report can say
  "we could not check" rather than "nothing was found".
"""

from __future__ import annotations

import random
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests

from gem_intel.config import Settings
from gem_intel.models import AccessIssue
from gem_intel.observability import get_logger, now_ist

log = get_logger(__name__)

# Phrases that mean "a human challenge is in front of you". Seeing any of
# these ends the crawl for that source.
CAPTCHA_MARKERS = (
    "g-recaptcha",
    "recaptcha/api.js",
    "hcaptcha",
    "captcha_code",
    "enter the captcha",
    "please verify you are a human",
    "cf-challenge",
    "__cf_chl",
    "incapsula incident id",
    "access denied by security policy",
)


class AccessBlocked(RuntimeError):
    """Raised when continuing would require circumventing an access control."""

    def __init__(self, message: str, url: str, kind: str = "captcha") -> None:
        super().__init__(message)
        self.url = url
        self.kind = kind


class DisallowedHost(RuntimeError):
    """Raised when a URL is outside the verified-source allow-list."""


@dataclass
class FetchResult:
    url: str
    status_code: int
    text: str = ""
    content: bytes = b""
    content_type: str = ""
    elapsed_ms: int = 0
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class RateLimiter:
    """Token-bucket-ish limiter: at most N requests per minute, with a floor
    delay between consecutive requests plus jitter."""

    def __init__(self, per_minute: int, min_delay: float, max_delay: float) -> None:
        self.interval = 60.0 / max(per_minute, 1)
        self.min_delay = min_delay
        self.max_delay = max(max_delay, min_delay)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            floor = max(self.interval, self.min_delay)
            target = self._last + floor + random.uniform(0, self.max_delay - self.min_delay)
            now = time.monotonic()
            if now < target:
                time.sleep(target - now)
            self._last = time.monotonic()


class RobotsPolicy:
    """robots.txt gate. Fail-closed: if robots cannot be read, we assume the
    crawl is not permitted rather than assuming it is."""

    def __init__(self, user_agent: str, enabled: bool = True) -> None:
        self.user_agent = user_agent
        self.enabled = enabled
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _parser_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._parsers:
            return self._parsers[origin]

        parser = urllib.robotparser.RobotFileParser()
        robots_url = urljoin(origin, "/robots.txt")
        try:
            response = requests.get(
                robots_url,
                headers={"User-Agent": self.user_agent},
                timeout=20,
            )
            if response.status_code == 404:
                # No robots.txt at all: RFC 9309 says everything is allowed.
                parser.parse([])
                log.info("no robots.txt; treating as unrestricted", origin=origin)
            elif response.ok:
                parser.parse(response.text.splitlines())
                log.info("robots.txt loaded", origin=origin)
            else:
                log.warning("robots.txt unreadable; failing closed",
                            origin=origin, status=response.status_code)
                self._parsers[origin] = None
                return None
        except requests.RequestException as exc:
            log.warning("robots.txt fetch failed; failing closed",
                        origin=origin, error=str(exc))
            self._parsers[origin] = None
            return None

        self._parsers[origin] = parser
        return parser

    def allows(self, url: str) -> bool:
        if not self.enabled:
            return True
        parser = self._parser_for(url)
        if parser is None:
            return False
        return parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        parser = self._parser_for(url)
        if parser is None:
            return None
        try:
            delay = parser.crawl_delay(self.user_agent)
        except AttributeError:      # pragma: no cover - very old stdlib
            return None
        return float(delay) if delay else None


class GemHttpClient:
    """The only object in the system permitted to talk to the network."""

    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.allowed_hosts = settings.allowed_hosts
        self.user_agent = settings.user_agent
        self.timeout = int(settings.get("source.request_timeout_seconds", 45))
        self.max_retries = int(settings.get("source.max_retries", 4))
        self.backoff_base = float(settings.get("source.backoff_base_seconds", 2.0))
        self.abort_on_captcha = bool(settings.get("source.abort_on_captcha", True))
        self.limiter = RateLimiter(
            per_minute=int(settings.get("source.requests_per_minute", 20)),
            min_delay=float(settings.get("source.min_delay_seconds", 2.0)),
            max_delay=float(settings.get("source.max_delay_seconds", 5.0)),
        )
        self.robots = RobotsPolicy(
            self.user_agent,
            enabled=bool(settings.get("source.respect_robots_txt", True)),
        )
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        self.access_issues: list[AccessIssue] = []
        self.requests_made = 0

    # -- guards ---------------------------------------------------------
    def assert_allowed_host(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            raise DisallowedHost(f"URL has no host: {url!r}")
        if host in self.allowed_hosts:
            return
        # Allow subdomains of an allow-listed apex (e.g. foo.gem.gov.in).
        if any(host.endswith("." + allowed) for allowed in self.allowed_hosts):
            return
        raise DisallowedHost(
            f"Refusing to fetch {host!r}: not an official GeM host. "
            f"Allowed: {sorted(self.allowed_hosts)}"
        )

    @staticmethod
    def looks_like_captcha(text: str) -> bool:
        lowered = text[:20000].lower()
        return any(marker in lowered for marker in CAPTCHA_MARKERS)

    # -- issue recording ------------------------------------------------
    def record_issue(self, stage: str, target: str, error_type: str,
                     detail: str = "", attempts: int = 0) -> AccessIssue:
        issue = AccessIssue(
            occurred_at=now_ist(self.settings.timezone),
            stage=stage, target=target, error_type=error_type,
            detail=detail, attempts=attempts,
        )
        self.access_issues.append(issue)
        log.warning("access issue recorded", stage=stage, target=target,
                    error_type=error_type, detail=detail[:300])
        return issue

    # -- core fetch -----------------------------------------------------
    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict | None = None,
        data: dict | None = None,
        stage: str = "fetch",
        binary: bool = False,
        max_bytes: int | None = None,
    ) -> FetchResult | None:
        """Fetch a URL, or return ``None`` after recording an access issue.

        Returning ``None`` rather than raising is deliberate: a single
        unreachable tender must degrade that tender, not the whole run.
        """
        self.assert_allowed_host(url)

        if not self.robots.allows(url):
            self.record_issue(stage, url, "ROBOTS_DISALLOWED",
                              "robots.txt does not permit this path for our user-agent")
            return None

        robots_delay = self.robots.crawl_delay(url)
        if robots_delay and robots_delay > self.limiter.min_delay:
            # The site asked for more space than we planned to give. Obey it.
            self.limiter.min_delay = robots_delay
            self.limiter.max_delay = max(self.limiter.max_delay, robots_delay)

        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            self.limiter.wait()
            started = time.monotonic()
            try:
                response = self.session.request(
                    method, url, params=params, data=data,
                    timeout=self.timeout, stream=binary,
                )
                self.requests_made += 1

                if response.status_code in (403, 401):
                    body = "" if binary else response.text[:2000]
                    if self.looks_like_captcha(body):
                        raise AccessBlocked("CAPTCHA / bot challenge presented", url)
                    self.record_issue(stage, url, f"HTTP_{response.status_code}",
                                      "Portal refused the request", attempt)
                    return None

                if response.status_code == 429 or 500 <= response.status_code < 600:
                    last_error = f"HTTP {response.status_code}"
                    retry_after = response.headers.get("Retry-After")
                    self._backoff(attempt, retry_after)
                    continue

                content_type = response.headers.get("Content-Type", "").split(";")[0].strip()

                if binary:
                    payload = self._read_capped(response, max_bytes)
                    if payload is None:
                        self.record_issue(stage, url, "DOCUMENT_TOO_LARGE",
                                          f"exceeded {max_bytes} bytes", attempt)
                        return None
                    return FetchResult(
                        url=response.url, status_code=response.status_code,
                        content=payload, content_type=content_type,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )

                text = response.text
                if self.looks_like_captcha(text):
                    raise AccessBlocked("CAPTCHA / bot challenge presented", url)

                return FetchResult(
                    url=response.url, status_code=response.status_code,
                    text=text, content_type=content_type,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            except AccessBlocked:
                # Never retried, never worked around.
                self.record_issue(
                    stage, url, "CAPTCHA_OR_BOT_CHALLENGE",
                    "Portal presented a human-verification challenge. "
                    "The automation stops here by design; run the query manually "
                    "or use an authorised access route.",
                    attempt,
                )
                if self.abort_on_captcha:
                    raise
                return None

            except requests.Timeout as exc:
                last_error = f"timeout: {exc}"
                self._backoff(attempt)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._backoff(attempt)

        self.record_issue(stage, url, "NETWORK_FAILURE", last_error, self.max_retries)
        return None

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 120.0))
                return
            except ValueError:
                pass
        delay = self.backoff_base ** attempt + random.uniform(0, 1.0)
        log.debug("backing off", attempt=attempt, seconds=round(delay, 1))
        time.sleep(min(delay, 120.0))

    @staticmethod
    def _read_capped(response: requests.Response, max_bytes: int | None) -> bytes | None:
        if max_bytes is None:
            return response.content
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > max_bytes:
                response.close()
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> GemHttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
