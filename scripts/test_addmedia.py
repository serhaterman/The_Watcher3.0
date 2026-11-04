"""/addmedia — add an account in media-only mode directly (2026-11-05).

Media-only (alerts for stories, posts and the avatar; follower, bio and name
changes stay quiet) could only be switched on from the account card, after the
account had been added and had already run its first check in full mode.

What must hold:
- /addmedia takes what /add takes — @username, profile URL, numeric id, or
  several at once — and the account is media-only BEFORE its first check;
- on an account already monitored it switches the mode, and adds nothing;
- without a target it explains itself and adds nothing;
- plain /add is unchanged, and an unauthorized user gets nowhere;
- the command is registered and listed in the bot's command menu.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_addmedia.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.bot import handlers  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import Base  # noqa: E402
from app.database.session import dispose_engine, engine, get_session  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def make_update(user_id: int = 99) -> SimpleNamespace:
    msg = SimpleNamespace(reply_text=AsyncMock(), message_id=1)
    return SimpleNamespace(
        callback_query=None,
        message=msg,
        effective_chat=SimpleNamespace(id=7),
        effective_user=SimpleNamespace(id=user_id),
    )


def make_context(service, args: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        user_data={},
        args=args,
        application=SimpleNamespace(bot_data={"monitor": service}),
    )


class FirstCheck:
    """Stands in for check_username and records, at the moment each first
    check starts, whether the account was already media-only."""

    def __init__(self) -> None:
        self.mode_at_check: dict[str, bool] = {}

    async def __call__(self, username: str, **kw) -> dict:
        async with get_session() as session:
            account = await crud.get_account(session, username)
            self.mode_at_check[username] = await crud.is_media_only(session, account.id)
        return {"ok": False, "error": "offline in this test"}


def make_service() -> SimpleNamespace:
    return SimpleNamespace(
        check_username=FirstCheck(),
        instagram=SimpleNamespace(
            fetch_username_by_id=AsyncMock(return_value="resolvedbyid")
        ),
    )


def replies(update) -> list[str]:
    return [c.args[0] for c in update.message.reply_text.await_args_list]


async def media_only(username: str):
    async with get_session() as session:
        account = await crud.get_account(session, username)
        if account is None:
            return None
        return await crud.is_media_only(session, account.id)


async def test_addmedia_adds_in_media_only_mode() -> None:
    service = make_service()
    update = make_update()
    await handlers.cmd_addmedia(update, make_context(service, ["@MediaOne"]))
    expect("the account is added, media-only", await media_only("mediaone") is True,
           repr(await media_only("mediaone")))
    expect("and was media-only before its first check ran",
           service.check_username.mode_at_check.get("mediaone") is True,
           repr(service.check_username.mode_at_check))
    expect("the reply says which alerts it will get",
           any("stories, posts and avatar only" in r for r in replies(update)),
           repr(replies(update)))

    by_url = make_service()
    await handlers.cmd_addmedia(
        make_update(), make_context(by_url, ["https://www.instagram.com/media.url/"])
    )
    expect("a profile URL works like /add's", await media_only("media.url") is True)

    by_id = make_service()
    await handlers.cmd_addmedia(make_update(), make_context(by_id, ["1234567890"]))
    expect("a numeric id is resolved and added media-only",
           await media_only("resolvedbyid") is True)


async def test_addmedia_switches_an_account_already_monitored() -> None:
    async with get_session() as session:
        await crud.add_account(session, "already_here", added_by=1)
    service = make_service()
    update = make_update()
    await handlers.cmd_addmedia(update, make_context(service, ["already_here"]))
    expect("an account already monitored is switched to media-only",
           await media_only("already_here") is True)
    expect("and told so", any("now alerting only" in r for r in replies(update)),
           repr(replies(update)))
    async with get_session() as session:
        same = [a for a in await crud.list_accounts(session, only_active=False)
                if a.username == "already_here"]
    expect("without adding it twice", len(same) == 1, repr(same))


async def test_addmedia_in_bulk() -> None:
    service = make_service()
    update = make_update()
    await handlers.cmd_addmedia(update, make_context(service, ["bulk_a", "bulk_b,bulk_c"]))
    modes = [await media_only(u) for u in ("bulk_a", "bulk_b", "bulk_c")]
    expect("every account of a bulk /addmedia is media-only",
           modes == [True, True, True], repr(modes))
    expect("the summary says so",
           any("stories, posts and avatar only" in r for r in replies(update)),
           repr(replies(update)))
    for _ in range(50):
        if len(service.check_username.mode_at_check) == 3:
            break
        await asyncio.sleep(0.02)
    expect("each first check ran media-only",
           service.check_username.mode_at_check
           == {"bulk_a": True, "bulk_b": True, "bulk_c": True},
           repr(service.check_username.mode_at_check))


async def test_addmedia_without_a_target_explains_itself() -> None:
    async with get_session() as session:
        before = len(await crud.list_accounts(session, only_active=False))
    update = make_update()
    await handlers.cmd_addmedia(update, make_context(make_service(), []))
    async with get_session() as session:
        after = len(await crud.list_accounts(session, only_active=False))
    expect("usage is shown", any("Usage" in r for r in replies(update)), repr(replies(update)))
    expect("and nothing is added", before == after, f"{before} -> {after}")


async def test_plain_add_is_unchanged() -> None:
    service = make_service()
    await handlers.cmd_add(make_update(), make_context(service, ["plainone"]))
    expect("/add keeps an account on every profile change",
           await media_only("plainone") is False, repr(await media_only("plainone")))


async def test_an_unauthorized_user_gets_nowhere() -> None:
    old = settings.telegram_admin_ids
    settings.telegram_admin_ids = "12345"
    try:
        update = make_update(user_id=99)
        await handlers.cmd_addmedia(update, make_context(make_service(), ["intruder"]))
    finally:
        settings.telegram_admin_ids = old
    expect("a non-admin adds nothing", await media_only("intruder") is None)


def test_the_command_is_registered() -> None:
    added: list[str] = []
    app = SimpleNamespace(
        add_handler=lambda h: added.extend(sorted(getattr(h, "commands", ()))),
        add_error_handler=lambda h: None,
    )
    handlers.register_handlers(app)
    expect("/addmedia has a handler", "addmedia" in added, repr(added))
    expect("and is in the command menu",
           any(c.command == "addmedia" for c in handlers.BOT_COMMANDS))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_addmedia_adds_in_media_only_mode()
    await test_addmedia_switches_an_account_already_monitored()
    await test_addmedia_in_bulk()
    await test_addmedia_without_a_target_explains_itself()
    await test_plain_add_is_unchanged()
    await test_an_unauthorized_user_gets_nowhere()
    test_the_command_is_registered()

    await dispose_engine()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All /addmedia checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
