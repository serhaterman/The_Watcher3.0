"""A "doesn't exist" alert is corrected when the account answers again.

On 2026-11-04 Instagram's username API began answering 404 for every
username, @instagram included, and six live targets were told "Instagram
says this username doesn't exist". Once the bot read them again it went
back to normal readings in silence: nothing ever told the chat the alert no
longer held, so it stood for good.

What must hold:
- the first successful reading after a "not found" alert sends one
  "answers again" message, in the sweep and in any other check;
- it is sent once per alert, never again on later readings;
- an account that was never reported missing gets nothing.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
DB_FILE = ROOT / "test_found_again.db"
if DB_FILE.exists():
    DB_FILE.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.database import crud  # noqa: E402
from app.database.models import Base, MonitoredAccount  # noqa: E402
from app.database.session import engine, get_session  # noqa: E402
from app.monitor.instagram import IdProbe, ProfileFetchResult  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


class ScriptedInstagram:
    """Every account answers on the page; the id route and the reel query
    are refused, as they were on 2026-11-04."""

    def __init__(self) -> None:
        self.direct_page_door_failing = False
        self.direct_page_failures = 0

    async def fetch_profile(self, username: str, **kw) -> ProfileFetchResult:
        return ProfileFetchResult(
            username=username, http_status=200, source="public_page",
            api_status=401,
            parsed={
                "username": username, "followers_count": 10,
                "following_count": 5, "is_private": False,
                "has_public_story": False,
            },
        )

    async def probe_by_id(self, user_id: str, **kw) -> IdProbe:
        return IdProbe(user_id=str(user_id), status=401)

    async def probe_public_page(self, username: str, **kw) -> dict:
        return {"status": 0, "parsed": None}

    async def fetch_reel_user(self, user_id: str):
        return None

    def reel_in_hand(self, user_id: str):
        return None

    async def fetch_hd_pic_url(self, user_id: str):
        return None


class QuietStories:
    async def fetch_stories(self, username):
        return []

    async def fetch_highlight_items(self, username, highlight_id, title):
        return []

    async def fetch_profile_pic_url(self, username):
        return None

    async def fetch_posts(self, username, limit: int = 12):
        return []

    def refusal_since(self, since):
        return None


def _service(*, stories=None) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_document = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=ScriptedInstagram(),
        hasher=AsyncMock(hash_url=AsyncMock(return_value=None)),
        notifier=notifier, stories=stories,
    )


def _corrections(service) -> list[str]:
    return [
        c.args[0] for c in service.notifier.send_text.await_args_list
        if "answers again" in c.args[0]
    ]


async def _new_account(username: str, instagram_id: Optional[str]) -> int:
    async with get_session() as session:
        account = MonitoredAccount(
            username=username, active=True, instagram_id=instagram_id
        )
        session.add(account)
        await session.flush()
        return account.id


async def _report_missing(service: MonitorService, account_id: int, username: str) -> None:
    """Two failed checks that say "User not found": the second one is what
    sends the "doesn't exist" alert, through the real failure path."""
    gone = ProfileFetchResult(
        username=username, http_status=404, error="User not found (public page)",
        source="public_page",
    )
    for _ in range(2):
        await service._handle_failure(
            account_id, username, gone, id_probe=IdProbe(user_id="1", status=401)
        )


async def _old_alert(account_id: int) -> None:
    """A "doesn't exist" alert logged before corrections existed — what
    every account's history held on 2026-11-05, up to 90 days back."""
    async with get_session() as session:
        await crud.log_notification(
            session, account_id=account_id, change_type="not_found",
            payload={"status": 404}, message="❓ doesn't exist", delivered=True,
        )


async def _answered(account_id: int) -> None:
    """What a successful reading records about the account."""
    async with get_session() as session:
        await crud.mark_checked(session, account_id, 200, success=True)


def _alerts(service) -> list[str]:
    return [
        c.args[0] for c in service.notifier.send_text.await_args_list
        if "doesn't exist" in c.args[0] and "answers again" not in c.args[0]
    ]


async def test_an_alert_is_corrected_once() -> None:
    account_id = await _new_account("wasmissing", "4500")
    service = _service()

    await service._clear_not_found(account_id, "wasmissing")
    expect("nothing to correct before any alert", _corrections(service) == [])

    await _report_missing(service, account_id, "wasmissing")
    expect("the failure path sent the alert", len(_alerts(service)) == 1,
           repr(_alerts(service)))
    await service._clear_not_found(account_id, "wasmissing")
    expect("one correction after the alert",
           len(_corrections(service)) == 1, repr(_corrections(service)))
    await _answered(account_id)
    await service._clear_not_found(account_id, "wasmissing")
    expect("and only one", len(_corrections(service)) == 1, repr(_corrections(service)))

    await _report_missing(service, account_id, "wasmissing")
    await service._clear_not_found(account_id, "wasmissing")
    expect("a new alert earns a new correction",
           len(_corrections(service)) == 2, repr(_corrections(service)))
    async with get_session() as session:
        notes = await crud.recent_notifications(session, account_id, limit=1)
    expect("the correction is on record",
           bool(notes) and notes[0].change_type == "found_again",
           repr([n.change_type for n in notes]))


async def test_an_old_alert_is_not_corrected() -> None:
    """The flood of 2026-11-05: the first sweep after corrections shipped
    sent "answers again" to all 17 accounts, because each had some old
    "doesn't exist" alert in its 90-day history with no correction after it.
    Only an alert raised since the account last answered may be corrected."""
    account_id = await _new_account("longago", "4550")
    await _old_alert(account_id)
    service = _service()
    await service._clear_not_found(account_id, "longago")
    expect("an alert from before the account last answered gets no correction",
           _corrections(service) == [], repr(_corrections(service)))


async def test_the_sweep_corrects_the_accounts_it_reported_missing() -> None:
    """The sweep that reads them again is what speaks — once — and only for
    the account the chat was told about in the failure that just ended."""
    async with get_session() as session:
        for a in await crud.list_accounts(session, only_active=True):
            await crud.set_account_active(session, a.username, False)
    service = _service(stories=QuietStories())
    missing = await _new_account("toldmissing", "4601")
    await _report_missing(service, missing, "toldmissing")
    stale = await _new_account("oldalert", "4602")
    await _old_alert(stale)
    await _new_account("neverreported", "4603")

    await service.check_all()
    first = _corrections(service)
    expect("one correction in the sweep that reads it again",
           len(first) == 1 and "toldmissing" in first[0], repr(first))
    await service.check_all()
    expect("and none in the next one", _corrections(service) == first,
           repr(_corrections(service)))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_an_alert_is_corrected_once()
    await test_an_old_alert_is_not_corrected()
    await test_the_sweep_corrects_the_accounts_it_reported_missing()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All found-again checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
