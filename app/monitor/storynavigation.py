"""storynavigation.com: highlight media, fresh copies only.

saveinsta has answered "Video is private." for every highlight of every
public account since 2026-10-23, and Instagram's own logged-out highlight page
carries no items. storynavigation's viewer still plays highlights: its page
posts {highlightId} to /get-highlight-stories with the Laravel CSRF token any
visitor gets with the page, and receives the reel's items as Instagram CDN
links (images at full size). No captcha or other bot check sits in that flow
and no login of ours is involved: the bot makes the requests the page makes.

What it answers is not always live (measured 2026-11-05). The first request
for a reel fetches it from Instagram, in 3.5-11.5 s, and that answer — the
items, or an empty list when the fetch failed (4 of 8 tries) — is then served
from storage for a day or more, in 0.2-0.33 s with the same signed links
every time. The owner's rule is fresh data or an honest failure, so a copy
counts only when this very request fetched it (it took seconds), or when it is
the copy this client fetched live within the last 15 minutes (a sweep's
listing, then a download). Anything else is refused, with its age when known.

Its own highlight LIST is never used: for a small account it answered []
while Instagram's page listed three. The catalog comes from Instagram; this is
asked for one known reel, and an empty answer is a failure, never "no items".
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from app.monitor.health import ERROR, OK, STORYNAV, fetch_health
from app.utils.logger import logger

_BASE = "https://storynavigation.com"
_HOME = f"{_BASE}/"
_HIGHLIGHT_URL = f"{_BASE}/get-highlight-stories"
_CSRF_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
_CHALLENGE_RE = re.compile(r"Just a moment|cf-chl|challenge-platform", re.I)
# The session cookie a token is bound to lives two hours (Max-Age=7200).
_TOKEN_TTL = 5400.0
# Their backend takes up to ~11 s per reel, fetching it from Instagram; two at
# a time keeps an account's whole catalog from landing on it at once.
_CONCURRENCY = 2
# A stored copy answers in 0.2-0.33 s, a fetch from Instagram takes 3.5 s or
# more. Anything quicker than this is taken as stored: a live answer refused
# by mistake is an honest failure, a stored one let through would not be.
_LIVE_MIN_SECONDS = 2.5
# How long the copy this client fetched live still counts as current.
_OWN_COPY_SECONDS = 900.0


class _NoToken(Exception):
    """The page that issues the CSRF token did not give one."""


class StoryNavigation:
    """One highlight reel's media from storynavigation.com, fresh copies only."""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._token_until = 0.0
        self._token_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(_CONCURRENCY)
        # highlight id -> (the signatures of the copy fetched live, when)
        self._live_copies: dict[str, tuple[frozenset[str], float]] = {}
        # (monotonic time, reason) of the last reel it did not serve.
        self._last_failure: Optional[tuple[float, str]] = None

    def failure_since(self, since: float) -> Optional[str]:
        """Why the last reel asked at or after `since` (a time.monotonic()
        value) came back with nothing usable; None when none failed since."""
        if self._last_failure is not None and self._last_failure[0] >= since:
            return self._last_failure[1]
        return None

    async def highlight_media(self, session: Any, highlight_id: str) -> list[tuple[str, str]]:
        """(media type, URL) for each item of the reel, in order; [] when it
        has no fresh copy, the reason kept for failure_since. `session` is the
        caller's curl_cffi session, so a test that swaps the caller's session
        swaps this one too."""
        numeric = str(highlight_id).split(":")[-1]
        try:
            async with self._slots:
                response, took = await self._ask(session, numeric)
                if response.status_code == 419:
                    # Laravel's "CSRF token mismatch": the session behind the
                    # token ended early. A fresh token, and one more try.
                    self._token = None
                    response, took = await self._ask(session, numeric)
        except _NoToken as exc:
            return self._fail(numeric, str(exc))
        except Exception as exc:
            fetch_health.record(STORYNAV, ERROR)
            return self._fail(numeric, f"could not be reached ({type(exc).__name__})")
        if response.status_code != 200:
            fetch_health.record_status(STORYNAV, response.status_code)
            return self._fail(numeric, _refusal(response))
        media = _media(response)
        if not media:
            fetch_health.record(STORYNAV, ERROR)
            return self._fail(numeric, "sent nothing for this highlight")
        stale = self._staleness(numeric, media, took)
        if stale:
            fetch_health.record(STORYNAV, ERROR)
            return self._fail(numeric, stale)
        fetch_health.record(STORYNAV, OK)
        return [(kind, url) for kind, url, _ in media]

    async def _ask(self, session: Any, numeric: str) -> tuple[Any, float]:
        token = await self._csrf_token(session)
        start = time.monotonic()
        response = await session.post(
            _HIGHLIGHT_URL,
            headers={
                "X-CSRF-TOKEN": token,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": _BASE,
                "Referer": _HOME,
            },
            data=json.dumps({"highlightId": f"highlight:{numeric}"}),
        )
        return response, time.monotonic() - start

    async def _csrf_token(self, session: Any) -> str:
        if self._token and time.monotonic() < self._token_until:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_until:
                return self._token
            page = await session.get(_HOME)
            if page.status_code != 200:
                fetch_health.record_status(STORYNAV, page.status_code)
                raise _NoToken(_refusal(page))
            match = _CSRF_RE.search(page.text or "")
            if match is None:
                fetch_health.record(STORYNAV, ERROR)
                raise _NoToken(
                    _refusal(page) if _CHALLENGE_RE.search(page.text or "")
                    else "sent its page without the token its requests need"
                )
            self._token, self._token_until = match.group(1), time.monotonic() + _TOKEN_TTL
            return self._token

    def _staleness(self, numeric: str, media: list[tuple[str, str, str]], took: float) -> Optional[str]:
        """Why this copy is not current, or None when it is."""
        signatures = frozenset(signature for _, _, signature in media)
        now = time.monotonic()
        if took >= _LIVE_MIN_SECONDS:
            self._live_copies = {
                hid: copy for hid, copy in self._live_copies.items()
                if now - copy[1] <= _OWN_COPY_SECONDS
            }
            self._live_copies[numeric] = (signatures, now)
            return None
        own = self._live_copies.get(numeric)
        if own is not None and own[0] == signatures:
            age = now - own[1]
            if age <= _OWN_COPY_SECONDS:
                return None
            return f"only had the copy it fetched {_ago(age)}"
        return "only had a stored copy, of unknown age"

    def _fail(self, numeric: str, reason: str) -> list:
        # Once per new reason: a sweep asks for every tracked reel, and the
        # same answer for each of them would bury the log.
        changed = self._last_failure is None or self._last_failure[1] != reason
        self._last_failure = (time.monotonic(), reason)
        (logger.info if changed else logger.debug)(
            "storynavigation did not serve highlight {}: {}", numeric, reason
        )
        return []


def _refusal(response: Any) -> str:
    """Why an answer carries no media, in words for the user."""
    if _CHALLENGE_RE.search(getattr(response, "text", "") or ""):
        return "was stopped by Cloudflare's bot check"
    return f"answered HTTP {response.status_code}"


def _media(response: Any) -> list[tuple[str, str, str]]:
    """(type, URL, signature) per usable entry of the playlist. Not JSON — an
    empty body is how a failed fetch from Instagram comes back — is none."""
    try:
        entries = response.json()
    except Exception:
        return []
    if not isinstance(entries, list):
        return []
    return [item for item in map(_item, entries) if item is not None]


def _item(entry: Any) -> Optional[tuple[str, str, str]]:
    """One playlist entry. A video's own file only: its thumbnail is a still,
    not the video. The signature (`oh`) tells one copy of the reel from
    another, since Instagram signs every fetch anew."""
    if not isinstance(entry, dict):
        return None
    kind = "video" if entry.get("type") == "video" else "image"
    url = _decode(entry.get("videoUrl" if kind == "video" else "thumbnailUrl"))
    if url is None:
        return None
    signature = parse_qs(urlsplit(url).query).get("oh", [url])[0]
    return kind, url, signature


def _decode(value: Any) -> Optional[str]:
    """A link as the page gets it: base64 of the URL (a plain one passes)."""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("https://"):
        return value
    try:
        url = base64.b64decode(value, validate=True).decode("ascii")
    except (binascii.Error, ValueError):
        return None
    return url if url.startswith("https://") else None


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"~{max(1, round(seconds / 60))} min ago"
    return f"~{round(seconds / 3600)} h ago"
