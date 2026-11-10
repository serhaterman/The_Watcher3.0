"""One account is never checked twice at once (2026-11-05).

Checks share a three-slot semaphore, and nothing kept two of them off the
same account: a sweep check, a stakeout tick and a card Recheck could all
land together. Each read the same previous snapshot, each found the same
change, and each announced it — and two story phases for one account both
loaded the seen-set before either marked anything, so the same story went
out twice.

What must hold:
- two checks of one account that overlap announce a change once;
- two story phases of one account that overlap deliver an item once;
- checks of DIFFERENT accounts still run side by side.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
DB_FILE = ROOT / "test_check_concurrency.db"
if DB_FILE.exists():
    DB_FILE.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.database.models import AccountSnapshot, Base, MonitoredAccount  # noqa: E402
from app.database.session import engine, get_session  # noqa: E402
from app.monitor.instagram import IdProbe, ProfileFetchResult  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402
from app.monitor.stories import StoryItem  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


class SlowInstagram:
    """Every account answers on the page with 20 followers, slowly enough
    that two checks started together are both in flight at once."""

    def __init__(self, delay: float = 0.3) -> None:
        self.delay = delay
        self.direct_page_door_failing = False
        self.direct_page_failures = 0

    async def fetch_profile(self, username: str, **kw) -> ProfileFetchResult:
        await asyncio.sleep(self.delay)
        return ProfileFetchResult(
            username=username, http_status=200, source="public_page",
            api_status=401,
            parsed={
                "username": username, "followers_count": 20,
                "following_count": 5, "is_private": True,
            },
        )

    async def probe_by_id(self, user_id: str, **kw) -> IdProbe:
        return IdProbe(user_id=str(user_id), status=401)

    async def fetch_reel_user(self, user_id: str):
        return None

    def reel_in_hand(self, user_id: str):
        return None

    async def fetch_hd_pic_url(self, user_id: str):
        return None


class SlowStories:
    """One story up; listing it takes long enough for two phases to overlap."""

    async def fetch_stories(self, username):
        await asyncio.sleep(0.2)
        return [StoryItem(pk="story-1", taken_at=0, media_type="image",
                          url="https://dl.example/1", source="story")]

    async def fetch_highlight_items(self, username, highlight_id, title):
        return []

    async def fetch_profile_pic_url(self, username):
        return None

    async def fetch_posts(self, username, limit: int = 12):
        return []

    async def download(self, item, username):
        return None

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


async def _account(username: str, followers: Optional[int] = None) -> int:
    async with get_session() as session:
        account = MonitoredAccount(username=username, active=True, instagram_id="42")
        session.add(account)
        await session.flush()
        if followers is not None:
            session.add(AccountSnapshot(
                account_id=account.id, username=username, http_status=200,
                followers_count=followers, following_count=5, is_private=True,
                raw_response={"source": "public_page"},
            ))
        return account.id


async def test_overlapping_checks_announce_a_change_once() -> None:
    """A sweep check and a stakeout tick on the same account at once."""
    account_id = await _account("doublecheck", followers=10)
    service = _service(SlowInstagram())
    await asyncio.gather(
        service._run_check(account_id, "doublecheck", paced=True),
        service._run_check(account_id, "doublecheck"),
    )
    gained = [t for t in _sent(service) if "gained" in t and "doublecheck" in t]
    expect("the follower change is announced once", len(gained) == 1, repr(gained))


async def test_overlapping_story_phases_deliver_once() -> None:
    account_id = await _account("doublestory")
    service = _service(SlowInstagram(), stories=SlowStories())
    delivered: list[str] = []

    async def deliver(aid, username, items, seen_pks, **kw):
        delivered.extend(item.pk for item in items)
        async with get_session() as session:
            from app.database import crud
            await crud.mark_story_items_seen(session, aid, items)
        return len(items)

    service._deliver_story_items = deliver
    # The first phase baselines; these two arrive together after it.
    await service._check_stories_and_highlights(
        account_id, "doublestory", instagram_id="42",
        reel_data={"has_public_story": False, "is_live": False, "highlights": {}},
    )
    from app.database import crud
    async with get_session() as session:
        await crud.set_setting(session, f"story_state:{account_id}", "none")
    reel = {"has_public_story": True, "is_live": False, "highlights": {}}
    await asyncio.gather(
        service._check_stories_and_highlights(
            account_id, "doublestory", instagram_id="42", reel_data=dict(reel)),
        service._check_stories_and_highlights(
            account_id, "doublestory", instagram_id="42", reel_data=dict(reel)),
    )
    expect("the story is delivered once", delivered == ["story-1"], repr(delivered))


async def test_different_accounts_still_run_together() -> None:
    first = await _account("sidebyside1", followers=10)
    second = await _account("sidebyside2", followers=10)
    service = _service(SlowInstagram(delay=0.5))
    started = time.monotonic()
    await asyncio.gather(
        service._run_check(first, "sidebyside1", paced=True),
        service._run_check(second, "sidebyside2", paced=True),
    )
    elapsed = time.monotonic() - started
    expect("two accounts are not serialized", elapsed < 0.95, f"{elapsed:.2f}s")


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_overlapping_checks_announce_a_change_once()
    await test_overlapping_story_phases_deliver_once()
    await test_different_accounts_still_run_together()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All check-concurrency checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
