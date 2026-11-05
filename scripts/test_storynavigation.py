"""storynavigation.com as the highlight media source: fresh copies only.

What must hold (measured 2026-11-05: the first request for a reel fetches it
from Instagram in seconds, then the same copy is served from storage for a day
or more in a fraction of one):
- an answer that took seconds is live and served;
- a quick answer is a stored copy and refused, saying so — unless it is the
  very copy this client fetched live within the last 15 minutes;
- an empty answer (their failed fetch, or the [] they then store) is a
  failure, never "this highlight is empty";
- a stale CSRF token is renewed once; every refusal says why;
- the client asks storynavigation first and saveinsta only when it has
  nothing, and the error names every source's reason.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.monitor import service as service_mod  # noqa: E402
from app.monitor import storynavigation as storynav_mod  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402
from app.monitor.stories import StoriesClient  # noqa: E402
from app.monitor.storynavigation import StoryNavigation  # noqa: E402

# The real line is 2.5 s; a test's "live" answer takes LIVE, a stored one 0.
storynav_mod._LIVE_MIN_SECONDS = 0.15
LIVE = 0.3

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def b64(url: str) -> str:
    return base64.b64encode(url.encode()).decode()


def video(n: int, oh: str = "s") -> dict:
    return {
        "type": "video", "createdTime": "2026-11-04 20:01:32",
        "videoUrl": b64(f"https://scontent-lga3-1.cdninstagram.com/o1/v/t2/f2/m86/clip{n}.mp4?oe=6AED65A1&oh={oh}{n}"),
        "thumbnailUrl": b64(f"https://scontent-lga3-1.cdninstagram.com/v/t51.71878-15/70{n}0001_18{n}000001_1_n.jpg?oh=t{n}"),
    }


def image(n: int, oh: str = "s") -> dict:
    return {
        "type": "image", "createdTime": "2026-04-10 15:39:05", "videoUrl": None,
        "thumbnailUrl": b64(f"https://scontent-lga3-1.cdninstagram.com/v/t51.71878-15/66{n}61457_18{n}99661067_1_n.jpg?oh={oh}{n}"),
    }


def url_of(entry: dict) -> str:
    return base64.b64decode(entry.get("videoUrl") or entry["thumbnailUrl"]).decode()


class Answer:
    def __init__(self, status: int = 200, body=None, *, text: str | None = None, delay: float = 0.0) -> None:
        self.status_code = status
        self.text = text if text is not None else ("" if body is None else json.dumps(body))
        self.delay = delay

    def json(self):
        return json.loads(self.text)


HOME = Answer(200, text='<html><head><meta name="csrf-token" content="tok1"></head></html>')


class Site:
    """storynavigation as the bot meets it: the home page with its token,
    then scripted answers to /get-highlight-stories."""

    def __init__(self, *answers: Answer, home: Answer = HOME) -> None:
        self.answers, self.home = list(answers), home
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict, dict]] = []
        self.in_flight = self.peak = 0

    async def get(self, url, **kw):
        self.gets.append(url)
        return self.home

    async def post(self, url, headers=None, data=None, **kw):
        self.posts.append((url, dict(headers or {}), json.loads(data)))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(answer.delay)
        finally:
            self.in_flight -= 1
        return answer

    async def close(self) -> None:
        pass


async def ask(site: Site, highlight_id: str = "18023864549864045", nav: StoryNavigation | None = None):
    nav = nav or StoryNavigation()
    since = time.monotonic()
    media = await nav.highlight_media(site, highlight_id)
    return media, nav.failure_since(since), nav


# ---------- live or stored -------------------------------------------------------

async def test_a_live_answer_is_served() -> None:
    playlist = [video(1), image(2)]
    site = Site(Answer(200, playlist, delay=LIVE))
    media, why, _ = await ask(site, "highlight:18023864549864045")
    expect("a live answer is served, in order",
           media == [("video", url_of(playlist[0])), ("image", url_of(playlist[1]))], repr(media))
    expect("with no failure on record", why is None, repr(why))
    url, headers, body = site.posts[0]
    expect("asked exactly as the page asks",
           url == "https://storynavigation.com/get-highlight-stories"
           and body == {"highlightId": "highlight:18023864549864045"}
           and headers.get("X-CSRF-TOKEN") == "tok1"
           and headers.get("X-Requested-With") == "XMLHttpRequest", repr(site.posts[0]))
    expect("the token came from its home page", site.gets == ["https://storynavigation.com/"], repr(site.gets))


async def test_a_stored_copy_is_refused() -> None:
    site = Site(Answer(200, [video(1), video(2)], delay=0.0))
    media, why, _ = await ask(site)
    expect("a quick answer is a stored copy: refused", media == [], repr(media))
    expect("and it says so", why == "only had a stored copy, of unknown age", repr(why))


async def test_its_own_live_copy_counts_for_fifteen_minutes() -> None:
    playlist = [video(1), video(2)]
    site = Site(Answer(200, playlist, delay=LIVE), Answer(200, playlist), Answer(200, playlist),
                Answer(200, [video(1, oh="other")]))
    nav = StoryNavigation()
    media, why, _ = await ask(site, nav=nav)
    expect("the live fetch is served", len(media) == 2 and why is None, repr((media, why)))
    media, why, _ = await ask(site, nav=nav)
    expect("the same copy soon after is still current (a listing, then a download)",
           len(media) == 2 and why is None, repr((media, why)))
    signatures, _ = nav._live_copies["18023864549864045"]
    nav._live_copies["18023864549864045"] = (signatures, time.monotonic() - 2000)
    media, why, _ = await ask(site, nav=nav)
    expect("past 15 minutes it is an old copy, with its age",
           media == [] and why == "only had the copy it fetched ~33 min ago", repr((media, why)))
    media, why, _ = await ask(site, nav=nav)
    expect("a quick copy that is not its own is of unknown age",
           media == [] and why == "only had a stored copy, of unknown age", repr((media, why)))


async def test_an_empty_answer_is_a_failure_not_an_empty_reel() -> None:
    site = Site(Answer(200, text="", delay=LIVE))
    media, why, _ = await ask(site)
    expect("their failed fetch (an empty body) serves nothing",
           media == [] and why == "sent nothing for this highlight", repr((media, why)))
    site = Site(Answer(200, [], delay=0.0))
    media, why, _ = await ask(site)
    expect("and so does the [] they store after it",
           media == [] and why == "sent nothing for this highlight", repr((media, why)))


# ---------- tokens and refusals --------------------------------------------------

async def test_a_stale_token_is_renewed_once() -> None:
    mismatch = Answer(419, {"message": "CSRF token mismatch."})
    site = Site(mismatch, Answer(200, [video(1)], delay=LIVE))
    media, why, _ = await ask(site)
    expect("a 419 renews the token and asks again",
           len(media) == 1 and len(site.gets) == 2 and len(site.posts) == 2, repr((media, site.gets)))
    site = Site(mismatch, Answer(419, {"message": "CSRF token mismatch."}))
    media, why, _ = await ask(site)
    expect("but only once", media == [] and why == "answered HTTP 419" and len(site.posts) == 2,
           repr((why, site.posts)))


async def test_every_refusal_says_why() -> None:
    cases = [
        (Site(Answer(403, text="<title>Just a moment...</title>")), "was stopped by Cloudflare's bot check"),
        (Site(Answer(500, text="oops")), "answered HTTP 500"),
        (Site(ConnectionError("reset")), "could not be reached (ConnectionError)"),
        (Site(home=Answer(200, text="<html>no token</html>")), "sent its page without the token its requests need"),
        (Site(home=Answer(503, text="down")), "answered HTTP 503"),
        (Site(home=Answer(403, text="<title>Just a moment...</title>")), "was stopped by Cloudflare's bot check"),
    ]
    for site, reason in cases:
        media, why, _ = await ask(site)
        expect(f"refused: {reason}", media == [] and why == reason, repr((media, why)))


async def test_entries_are_read_carefully() -> None:
    playlist = [
        {"type": "video", "videoUrl": None, "thumbnailUrl": video(1)["thumbnailUrl"]},  # no video file
        {"type": "video", "videoUrl": "not base64 at all!"},
        {"type": "image", "thumbnailUrl": b64("file:///etc/passwd")},
        "not an entry",
        {"type": "image", "thumbnailUrl": "https://scontent.cdninstagram.com/v/t51.2885-15/plain.jpg?oh=p"},
        image(5),
    ]
    site = Site(Answer(200, playlist, delay=LIVE))
    media, why, _ = await ask(site)
    expect("only usable links are kept: a video's still is never its video",
           media == [("image", "https://scontent.cdninstagram.com/v/t51.2885-15/plain.jpg?oh=p"),
                     ("image", url_of(image(5)))], repr(media))


async def test_one_token_and_two_at_a_time() -> None:
    site = Site(*(Answer(200, [video(n)], delay=LIVE) for n in range(5)))
    nav = StoryNavigation()
    results = await asyncio.gather(*(nav.highlight_media(site, f"1800000000000000{n}") for n in range(5)))
    expect("every reel served", all(len(r) == 1 for r in results), repr(results))
    expect("never more than two at once", site.peak == 2, repr(site.peak))
    expect("one token page for all of them", len(site.gets) == 1, repr(site.gets))


# ---------- the client and the service -------------------------------------------

async def test_the_client_asks_storynavigation_first() -> None:
    client = StoriesClient()
    await client._session.close()
    playlist = [image(7), video(8)]
    client._session = Site(Answer(200, playlist, delay=LIVE))  # type: ignore[assignment]
    client._fetch_media_html = AsyncMock(return_value="")  # type: ignore[method-assign]
    items = await client.fetch_highlight_items("nasa", "18195781759377100", "Roman")
    expect("its items come back as highlight items",
           [(i.media_type, i.url) for i in items] == [("image", url_of(playlist[0])), ("video", url_of(playlist[1]))]
           and all(i.source == "highlight" and i.highlight_id == "18195781759377100"
                   and i.highlight_title == "Roman" for i in items), repr(items))
    expect("keyed by the media id in the CDN path, as saveinsta's are",
           items[0].pk == "66761457", repr(items[0].pk))
    expect("and saveinsta is not asked", client._fetch_media_html.await_count == 0)

    client._session = Site(Answer(200, playlist))  # type: ignore[assignment]  # a stored copy
    client._storynav = StoryNavigation()
    since = time.monotonic()
    items = await client.fetch_highlight_items("nasa", "18195781759377100", "Roman")
    expect("with no fresh copy, saveinsta is asked for the reel",
           items == [] and client._fetch_media_html.await_args.args[0]
           == "https://www.instagram.com/stories/highlights/18195781759377100/",
           repr(client._fetch_media_html.await_args))
    expect("and storynavigation's reason is kept for the error",
           client.highlight_failure_since(since) == "only had a stored copy, of unknown age",
           repr(client.highlight_failure_since(since)))


class Reasons:
    def __init__(self, storynav, saveinsta) -> None:
        self.storynav, self.saveinsta = storynav, saveinsta

    def highlight_failure_since(self, since):
        return self.storynav

    def refusal_since(self, since):
        return self.saveinsta


def test_the_error_names_every_source() -> None:
    def error(storynav, saveinsta) -> str:
        service = MonitorService(instagram=AsyncMock(), hasher=AsyncMock(),
                                 notifier=AsyncMock(), stories=Reasons(storynav, saveinsta))
        return service._media_unavailable(0.0)

    both = error("only had a stored copy, of unknown age", "Video is private.")
    expect("both sources' reasons, storynavigation first", both == (
        "No anonymous source served it — storynavigation only had a stored copy, of unknown "
        "age; saveinsta refused it: “Video is private.” Highlight names and story status "
        "still work."), both)
    one = error("sent nothing for this highlight", None)
    expect("one reason reads as a sentence", one == (
        "No anonymous source served it — storynavigation sent nothing for this highlight. "
        "Highlight names and story status still work."), one)
    expect("saveinsta's alone still reads",
           "saveinsta refused it: “Video is private.”" in error(None, "Video is private."))
    expect("no reason at all keeps the old message",
           error(None, None) == service_mod._DOWNLOAD_UNAVAILABLE_MSG)


async def main() -> int:
    await test_a_live_answer_is_served()
    await test_a_stored_copy_is_refused()
    await test_its_own_live_copy_counts_for_fifteen_minutes()
    await test_an_empty_answer_is_a_failure_not_an_empty_reel()
    await test_a_stale_token_is_renewed_once()
    await test_every_refusal_says_why()
    await test_entries_are_read_carefully()
    await test_one_token_and_two_at_a_time()
    await test_the_client_asks_storynavigation_first()
    test_the_error_names_every_source()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All storynavigation tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
