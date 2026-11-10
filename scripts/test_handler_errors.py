"""A button whose handler fails gets an answer (2026-11-05).

The error handler only logged. A handler that raised left the tapped
button's spinner turning until Telegram gave up on it, with no word to the
person who tapped it of what had happened.

What must hold:
- a failed callback is answered, with an alert saying to try again;
- answering is best effort: a query Telegram will no longer accept, or an
  error with no update behind it, never raises out of the error handler.

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

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_handler_errors.db")

from telegram import CallbackQuery, Update, User  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

from app.bot import handlers  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def _tap(bot) -> Update:
    query = CallbackQuery(
        id="q1", from_user=User(id=1, first_name="owner", is_bot=False),
        chat_instance="c1", data="acct:someone",
    )
    query.set_bot(bot)
    return Update(update_id=1, callback_query=query)


async def test_a_failed_tap_is_answered() -> None:
    bot = AsyncMock()
    await handlers.on_error(_tap(bot), SimpleNamespace(error=RuntimeError("boom")))
    calls = bot.answer_callback_query.await_args_list
    expect("the tap is answered once", len(calls) == 1, repr(calls))
    if calls:
        kw = calls[0].kwargs
        expect("as an alert that says to try again",
               kw.get("show_alert") is True and "try again" in (kw.get("text") or ""),
               repr(kw))


async def test_answering_never_raises() -> None:
    bot = AsyncMock()
    bot.answer_callback_query.side_effect = BadRequest("Query is too old")
    raised = None
    try:
        await handlers.on_error(_tap(bot), SimpleNamespace(error=RuntimeError("boom")))
        await handlers.on_error(None, SimpleNamespace(error=RuntimeError("no update")))
    except Exception as exc:  # pragma: no cover - the failure being tested
        raised = exc
    expect("a stale query or a missing update is swallowed", raised is None, repr(raised))


async def main() -> int:
    await test_a_failed_tap_is_answered()
    await test_answering_never_raises()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All handler-error checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
