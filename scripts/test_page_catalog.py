"""The page carries what the refused reel query used to (2026-11-04).

By that day Instagram's reel query answered "require_login" even from a home
IP, and the Worker's reel route was refused per colo, so every due highlight
catalog cost the sweep a ~9 s refusal and the stored catalog aged. The
logged-out profile page lists a public account's whole highlight catalog
(`lox_highlights_connection`) and its post grid
(`polaris_ordered_timeline_connection`), on the page the sweep fetches anyway.

What must hold:
- the page's catalog is read only when Instagram says the list is complete,
  only for a public account, and never from a list with an unreadable entry;
- a check whose page listed the catalog spends no reel call, neither in the
  story phase nor from the sweep's catalog budget, and the stored catalog
  follows the page;
- a catalog changing source never reads an RTL title as renamed;
- the page's empty grid is a listing: stamped, and saveinsta not asked;
- the card and the download panel ask the page before the refused reel query;
- the page line counts profile fields, not the extras riding along.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
DB_FILE = ROOT / "test_page_catalog.db"
if DB_FILE.exists():
    DB_FILE.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import Base, MonitoredAccount  # noqa: E402
from app.database.session import engine, get_session  # noqa: E402
from app.monitor.instagram import (  # noqa: E402
    IdProbe, InstagramClient, ProfileFetchResult,
)
from app.monitor.public_page import parse_public_profile  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402
from app.monitor.stories import StoryItem  # noqa: E402
from app.utils.logger import logger  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


PK = "52584787030"
# Ids and titles as @whos.lisianna's page listed them on 2026-11-04; the
# second title carries the RLM marks some surfaces wrap around text.
REELS = (("18061985582607195", "☆"), ("17874420114681748", "‏♡‏"))


def page(
    *,
    private: bool = False,
    highlights: tuple = REELS,
    has_next: bool = False,
    grid: tuple = (),
    grid_next: bool = False,
    grid_pk: str = PK,
    grid_first: bool = False,
) -> str:
    """A page shaped like the 2026-11-04 captures: a profile block and a
    posts-grid block under the same key, in either order."""
    edges = ",".join(
        '{"node":{"id":"%s","title":%s,"owner_username":"lis",'
        '"__typename":"XIGHighlightReel"},"cursor":"c"}' % (hid, json.dumps(title))
        for hid, title in highlights
    )
    profile = (
        '{"pk":"%s","username":"lis","is_private":%s,"latest_reel_media":0,'
        '"biography":"b","full_name":"L","is_verified":false,"bio_links":[],'
        '"follower_count":127,"following_count":273,"all_media_count":null,'
        '"id":"17841452650903098","lox_highlights_connection":{"edges":[%s],'
        '"page_info":{"end_cursor":null,"has_next_page":%s}},'
        '"has_any_clips":false}'
        % (PK, json.dumps(private), edges, json.dumps(has_next))
    )
    posts = ",".join(
        '{"node":{"pk":"%s","code":"C%s","media_type":1}}' % (p, p) for p in grid
    )
    grid_block = (
        '{"pk":"%s","polaris_ordered_timeline_connection":{"edges":[%s],'
        '"page_info":{"end_cursor":null,"has_next_page":%s}},"id":"1784"}'
        % (grid_pk, posts, json.dumps(grid_next))
    )
    blocks = [grid_block, profile] if grid_first else [profile, grid_block]
    return "<!DOCTYPE html><html><body>" + "".join(
        '<script type="application/json" data-sjs>{"require":[['
        '"RelayPrefetchedStreamCache","next",[],[{"__bbox":{"result":{"data":'
        '{"xig_user_by_username":%s}}}}]]]}</script>' % block
        for block in blocks
    ) + "</body></html>"


# ---------- the parser ------------------------------------------------------

def test_a_public_accounts_whole_catalog_is_read() -> None:
    parsed = parse_public_profile(page(), "lis") or {}
    expect("the counts still parse", parsed.get("followers_count") == 127, repr(parsed))
    expect("the catalog is read, ids to titles, without bidi marks",
           parsed.get("highlights")
           == {"18061985582607195": "☆", "17874420114681748": "♡"},
           repr(parsed.get("highlights")))
    empty = parse_public_profile(page(highlights=()), "lis") or {}
    expect("a complete empty list is an empty catalog, not an unknown one",
           empty.get("highlights") == {}, repr(empty.get("highlights")))


def test_only_a_complete_list_is_a_catalog() -> None:
    paginated = parse_public_profile(page(has_next=True), "lis") or {}
    expect("a list with a next page is not a catalog",
           "highlights" not in paginated, repr(paginated.get("highlights")))
    broken = page().replace('"id":"17874420114681748"', '"id":null')
    spoiled = parse_public_profile(broken, "lis") or {}
    expect("one unreadable entry spoils the list",
           "highlights" not in spoiled, repr(spoiled.get("highlights")))


def test_a_private_accounts_lists_say_nothing() -> None:
    """A logged-out viewer sees a private account's lists as empty whatever
    they hold — an empty catalog read there would 'remove' every highlight."""
    parsed = parse_public_profile(page(private=True, highlights=()), "lis") or {}
    expect("the private account still parses", parsed.get("is_private") is True)
    expect("but yields no catalog", "highlights" not in parsed, repr(parsed))
    expect("and no grid verdict", "grid_empty" not in parsed, repr(parsed))


def test_the_grid_block_says_whether_there_are_posts() -> None:
    def grid_of(**kw) -> Any:
        return (parse_public_profile(page(**kw), "lis") or {}).get("grid_empty", "absent")

    expect("none listed and no next page = empty", grid_of() is True)
    expect("in either block order", grid_of(grid_first=True) is True)
    expect("a listed post = not empty", grid_of(grid=("1",)) is False)
    expect("none listed but a next page = unknown",
           grid_of(grid_next=True) == "absent")
    expect("another account's grid = unknown", grid_of(grid_pk="999") == "absent")


def test_the_page_line_counts_profile_fields_only() -> None:
    """It counted every key in the reading, so the story flag already made
    a 9-field reading say 10 — and the catalog and grid would make it 11."""
    captured: list[str] = []
    sink = logger.add(lambda message: captured.append(str(message)), level="INFO")
    try:
        InstagramClient._page_result(
            "lis", {"parsed": parse_public_profile(page(), "lis"), "door": "direct"}
        )
    finally:
        logger.remove(sink)
    line = next((c for c in captured if "answered on the public page" in c), "")
    expect("eight profile fields read, eight counted",
           "(8 of 11 fields)" in line, repr(line))


# ---------- the service -----------------------------------------------------

class ScriptedInstagram:
    """A client whose doors answer whatever a test scripts, and which
    records every reel query and page it is asked for."""

    def __init__(self) -> None:
        self.profile = lambda u: ProfileFetchResult(
            username=u, http_status=401, error="HTTP 401", api_status=401,
        )
        self.page_outcome = lambda u: {"status": 0, "parsed": None}
        self.reel_answer: Optional[dict] = None
        self.reel_calls: list[str] = []
        self.page_calls: list[str] = []
        self.direct_page_door_failing = False
        self.direct_page_failures = 0

    async def fetch_profile(self, username: str, **kw) -> ProfileFetchResult:
        return self.profile(username)

    async def probe_by_id(self, user_id: str, **kw) -> IdProbe:
        return IdProbe(user_id=str(user_id), status=401)

    async def probe_public_page(self, username: str, **kw) -> dict:
        self.page_calls.append(username)
        return self.page_outcome(username)

    async def fetch_reel_user(self, user_id: str):
        self.reel_calls.append(str(user_id))
        return self.reel_answer

    def reel_in_hand(self, user_id: str):
        return None

    async def fetch_hd_pic_url(self, user_id: str):
        return None


class RecordingStories:
    def __init__(self) -> None:
        self.post_calls: list[str] = []

    async def fetch_stories(self, username):
        return []

    async def fetch_highlight_items(self, username, highlight_id, title):
        return []

    async def fetch_profile_pic_url(self, username):
        return None

    async def fetch_posts(self, username, limit: int = 12):
        self.post_calls.append(username)
        return []

    def refusal_since(self, since):
        return None

    def highlight_failure_since(self, since):
        return None


def _service(instagram, *, stories=None) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_document = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=instagram,
        hasher=AsyncMock(hash_url=AsyncMock(return_value=None)),
        notifier=notifier, stories=stories,
    )


def _sent(service) -> list[str]:
    return [c.args[0] for c in service.notifier.send_text.await_args_list]


async def _new_account(username: str, instagram_id: Optional[str]) -> int:
    async with get_session() as session:
        account = MonitoredAccount(
            username=username, active=True, instagram_id=instagram_id
        )
        session.add(account)
        await session.flush()
        return account.id


async def _pause_everything() -> None:
    async with get_session() as session:
        for a in await crud.list_accounts(session, only_active=True):
            await crud.set_account_active(session, a.username, False)


def _page_reading(username: str, instagram_id: str, **extra) -> ProfileFetchResult:
    return ProfileFetchResult(
        username=username, http_status=200, source="public_page", api_status=401,
        parsed={
            "username": username, "followers_count": 10, "following_count": 5,
            "is_private": False, "instagram_id": instagram_id,
            "has_public_story": False, **extra,
        },
    )


async def test_a_listed_catalog_needs_no_reel_call() -> None:
    """Even a DUE catalog spends nothing when the page listed it: the
    stored catalog follows the page, and an added reel is announced."""
    account_id = await _new_account("pagecat", "4201")
    async with get_session() as session:
        await crud.replace_highlight_catalog(session, account_id, {"h1": "Trips"})
    ig = ScriptedInstagram()
    service = _service(ig, stories=RecordingStories())
    await service._check_stories_and_highlights(
        account_id, "pagecat", instagram_id="4201", catalog_due=True,
        reel_data={
            "has_public_story": False, "is_live": False, "from_page": True,
            "highlights": {"h1": "Trips", "h2": "Food"},
        },
    )
    expect("no reel call", ig.reel_calls == [], repr(ig.reel_calls))
    async with get_session() as session:
        stored = await crud.get_highlight_catalog(session, account_id)
    expect("the stored catalog follows the page",
           stored == {"h1": "Trips", "h2": "Food"}, repr(stored))
    texts = _sent(service)
    expect("and the added reel is announced",
           any("Added (1)" in t and "Food" in t for t in texts), repr(texts))


async def test_a_source_switch_does_not_rename_rtl_titles() -> None:
    """The reel query may keep the RLM marks the page strips; the first
    page-read catalog must not announce @opscn1's reel as renamed."""
    account_id = await _new_account("rtlcat", "4202")
    async with get_session() as session:
        await crud.replace_highlight_catalog(
            session, account_id, {"h1": "‏حسوني moshi‏"}
        )
    service = _service(ScriptedInstagram(), stories=RecordingStories())
    await service._check_stories_and_highlights(
        account_id, "rtlcat", instagram_id="4202",
        reel_data={
            "has_public_story": False, "is_live": False, "from_page": True,
            "highlights": {"h1": "حسوني moshi"},
        },
    )
    texts = _sent(service)
    expect("no 'highlights updated' message",
           not any("highlights updated" in t for t in texts), repr(texts))


async def test_a_sweep_reads_catalogs_and_empty_grids_from_the_page() -> None:
    """Two public accounts whose pages list their catalogs and show no posts:
    the sweep spends no reel call and no saveinsta listing on either, and
    stamps both grid scans as done. A second sweep, with the scans due again,
    still asks nothing and announces no change."""
    await _pause_everything()
    ids = {"listed1": "9101", "listed2": "9102"}
    accounts = {u: await _new_account(u, i) for u, i in ids.items()}
    ig = ScriptedInstagram()
    ig.profile = lambda u: _page_reading(
        u, ids[u], highlights={"h9": "Nine"}, grid_empty=True
    )
    stories = RecordingStories()
    service = _service(ig, stories=stories)

    captured: list[str] = []
    sink = logger.add(lambda message: captured.append(str(message)), level="INFO")
    try:
        await service.check_all()
        async with get_session() as session:
            for aid in accounts.values():
                await crud.delete_setting(session, f"post_scan:{aid}")
        first_texts = len(_sent(service))
        await service.check_all()
    finally:
        logger.remove(sink)

    expect("no reel call in either sweep", ig.reel_calls == [], repr(ig.reel_calls))
    expect("no catalog is counted as due a live re-read",
           not any("due a live re-read" in c for c in captured),
           repr([c for c in captured if "re-read" in c]))
    async with get_session() as session:
        stored = {
            u: await crud.get_highlight_catalog(session, aid)
            for u, aid in accounts.items()
        }
        stamps = [
            await crud.get_setting(session, f"post_scan:{aid}")
            for aid in accounts.values()
        ]
    expect("each catalog is stored from its page",
           all(c == {"h9": "Nine"} for c in stored.values()), repr(stored))
    expect("saveinsta is never asked for an empty grid",
           stories.post_calls == [], repr(stories.post_calls))
    expect("and the grid scan is stamped as done", all(stamps), repr(stamps))
    texts = _sent(service)
    expect("the second sweep announces no highlight change",
           not any("highlights updated" in t for t in texts[first_texts:]),
           repr(texts[first_texts:]))


async def test_the_card_asks_the_page_before_the_refused_reel_query() -> None:
    ig = ScriptedInstagram()
    ig.reel_answer = {"highlights": {"r1": "From reel"}}
    service = _service(ig)

    ig.page_outcome = lambda u: {"status": 200, "parsed": {
        "username": u, "is_private": False, "instagram_id": "4300",
        "highlights": {"p1": "From page"},
    }}
    catalog = await service._fetch_highlight_catalog("cardy", "4300")
    expect("a page that lists the catalog answers it",
           catalog == {"p1": "From page"}, repr(catalog))
    expect("without a reel call", ig.reel_calls == [], repr(ig.reel_calls))

    ig.page_outcome = lambda u: {"status": 200, "parsed": {
        "username": u, "is_private": False, "instagram_id": "4300",
    }}
    catalog = await service._fetch_highlight_catalog("cardy", None)
    expect("a page that does not list it hands over to the reel query",
           catalog == {"r1": "From reel"} and ig.reel_calls == ["4300"],
           repr((catalog, ig.reel_calls)))

    pages_before = len(ig.page_calls)
    await service._fetch_highlight_catalog("cardy", "4300", page=False)
    expect("page=False never asks the page",
           len(ig.page_calls) == pages_before, repr(ig.page_calls))


async def test_the_download_panel_uses_the_catalog_its_page_read_carried() -> None:
    ig = ScriptedInstagram()
    ig.profile = lambda u: _page_reading(u, "4400", highlights={"p2": "Two", "p1": "One"})
    service = _service(ig)
    overview = await service.get_download_overview("panelled")
    expect("the panel lists the page's catalog",
           overview.get("items") == [("p1", "One"), ("p2", "Two")], repr(overview))
    expect("with no reel call and no second page",
           ig.reel_calls == [] and ig.page_calls == [],
           repr((ig.reel_calls, ig.page_calls)))


class ListingStories(RecordingStories):
    """Highlight reels whose items a test scripts, reel by reel."""

    def __init__(self, items_by_reel: dict[str, list[str]]) -> None:
        super().__init__()
        self.items_by_reel = items_by_reel

    async def fetch_highlight_items(self, username, highlight_id, title):
        return [
            StoryItem(pk=pk, taken_at=0, media_type="image",
                      url=f"https://scontent.cdninstagram.com/v/{pk}.jpg", source="highlight",
                      highlight_id=highlight_id, highlight_title=title)
            for pk in self.items_by_reel.get(highlight_id, [])
        ]


async def test_highlight_media_coming_back_is_no_flood() -> None:
    """2026-11-05: highlight media came back (storynavigation) after two weeks
    of none, with item ids that need not match the ones recorded before. A
    reel already in the catalog has its first listing recorded quietly; a reel
    new to the catalog is news, however many of its listings fail first."""
    old_interval = settings.highlight_scan_interval
    settings.highlight_scan_interval = 0  # every check re-lists every reel
    try:
        account_id = await _new_account("quietreels", "4301")
        async with get_session() as session:
            await crud.replace_highlight_catalog(session, account_id, {"h1": "Trips", "h3": "Pets"})
            await crud.mark_story_items_seen(session, account_id, [StoryItem(
                pk="saveinsta-era", taken_at=0, media_type="image", url="x",
                source="highlight", highlight_id="h1")])
        stories = ListingStories({"h1": ["p1", "p2"], "h2": [], "h3": []})
        service = _service(ScriptedInstagram(), stories=stories)
        delivered: list[str] = []

        async def deliver(account_id, username, items, seen, **kw):
            delivered.extend(item.pk for item in items)
            return len(items)

        service._deliver_story_items = deliver  # type: ignore[method-assign]

        async def check() -> None:
            await service._check_stories_and_highlights(
                account_id, "quietreels", instagram_id="4301", catalog_due=True,
                reel_data={
                    "has_public_story": False, "is_live": False, "from_page": True,
                    "highlights": {"h1": "Trips", "h2": "New", "h3": "Pets"},
                },
            )

        await check()
        async with get_session() as session:
            seen = await crud.get_seen_story_pks(session, account_id)
            pending = await crud.get_setting(session, f"highlight_quiet_reels:{account_id}")
        expect("a pre-existing reel's first listing delivers nothing", delivered == [], repr(delivered))
        expect("but records its items as seen", {"p1", "p2"} <= set(seen), repr(seen))
        expect("an old reel not served yet still owes its quiet listing",
               json.loads(pending or "null") == ["h3"], repr(pending))

        stories.items_by_reel = {"h1": ["p1", "p2", "p3"], "h2": ["n1"], "h3": ["q1"]}
        await check()
        expect("then an item added to that reel is delivered", "p3" in delivered, repr(delivered))
        expect("so is the new reel's media, though its first listing failed",
               "n1" in delivered, repr(delivered))
        expect("while the other old reel's first listing stays quiet",
               "q1" not in delivered, repr(delivered))
        expect("and nothing else is delivered", sorted(delivered) == ["n1", "p3"], repr(delivered))
    finally:
        settings.highlight_scan_interval = old_interval


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_a_public_accounts_whole_catalog_is_read()
    test_only_a_complete_list_is_a_catalog()
    test_a_private_accounts_lists_say_nothing()
    test_the_grid_block_says_whether_there_are_posts()
    test_the_page_line_counts_profile_fields_only()
    await test_a_listed_catalog_needs_no_reel_call()
    await test_a_source_switch_does_not_rename_rtl_titles()
    await test_a_sweep_reads_catalogs_and_empty_grids_from_the_page()
    await test_the_card_asks_the_page_before_the_refused_reel_query()
    await test_the_download_panel_uses_the_catalog_its_page_read_carried()
    await test_highlight_media_coming_back_is_no_flood()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All page-catalog checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
