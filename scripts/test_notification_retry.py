"""Verify the notification send/retry logic, focused on the duplicate-upload bug.

Hand-rolled smoke test (no pytest). The production failure: a media upload
reaches Telegram but the response is slow, the request raises TimedOut, and the
old retry loop resent the same file 2â€“4Ã— (users got duplicate photos/videos).
The fix: uploads are not retried on TimedOut (assumed delivered), while text
sends and other Telegram errors keep retrying.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from telegram.error import BadRequest, RetryAfter, TimedOut  # noqa: E402

from app.bot.notifications import _UPLOAD_TIMEOUTS, NotificationDispatcher  # noqa: E402
from app.config import settings  # noqa: E402

FAILURES: list[str] = []

# A real on-disk file so the send methods' `open(path, "rb")` succeeds and the
# (mocked) bot.send_* call is actually reached.
_TMP = tempfile.NamedTemporaryFile(prefix="watcher-test-", suffix=".bin", delete=False)
_TMP.write(b"fake media bytes")
_TMP.close()
MEDIA = Path(_TMP.name)


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def make_dispatcher() -> NotificationDispatcher:
    bot = AsyncMock()
    return NotificationDispatcher(bot, chat_id=1)


async def test_upload_timeout_sends_once_no_retry() -> None:
    # A photo leaves as a file or as a photo (PHOTOS_AS_FILES); the duplicate
    # guard has to hold on whichever Telegram method carries it.
    for as_file, method in ((True, "send_document"), (False, "send_photo")):
        settings.photos_as_files = as_file
        d = make_dispatcher()
        setattr(d.bot, method, AsyncMock(side_effect=TimedOut("Timed out")))
        ok = await d.send_photo(MEDIA, caption="c")
        sent = getattr(d.bot, method)
        expect(
            f"photo upload ({method}) that times out is reported delivered "
            "(no false failure)",
            ok is True,
        )
        expect(
            f"photo upload ({method}) is attempted exactly once on timeout "
            "(no duplicate resend)",
            sent.await_count == 1,
            f"await_count={sent.await_count}",
        )
    settings.photos_as_files = True


async def test_video_timeout_sends_once() -> None:
    d = make_dispatcher()
    d.bot.send_video = AsyncMock(side_effect=TimedOut("Timed out"))
    ok = await d.send_video(MEDIA, caption="c")
    expect("video timeout -> delivered, once", ok and d.bot.send_video.await_count == 1)


async def test_document_timeout_sends_once() -> None:
    d = make_dispatcher()
    d.bot.send_document = AsyncMock(side_effect=TimedOut("Timed out"))
    ok = await d.send_document(MEDIA, caption="c")
    expect(
        "document timeout -> delivered, once",
        ok and d.bot.send_document.await_count == 1,
    )


async def test_upload_passes_generous_timeouts() -> None:
    d = make_dispatcher()
    d.bot.send_document = AsyncMock(return_value=None)
    await d.send_photo(MEDIA, caption="c")
    kwargs = d.bot.send_document.call_args.kwargs
    expect(
        "upload passes the large write/read timeout kwargs",
        all(kwargs.get(k) == v for k, v in _UPLOAD_TIMEOUTS.items()),
        f"kwargs={kwargs}",
    )
    expect("write timeout is well above the 5s default", _UPLOAD_TIMEOUTS["write_timeout"] >= 60)


async def test_upload_success_path() -> None:
    d = make_dispatcher()
    d.bot.send_photo = AsyncMock(return_value=None)
    hook = AsyncMock()
    d.post_send_hook = hook
    ok = await d.send_photo(MEDIA)
    expect("successful photo returns True and fires the panel hook", ok and hook.await_count == 1)


async def test_text_timeout_still_retries() -> None:
    # Text sends are small and a timeout usually means it didn't go through, so
    # retrying is correct there â€” only uploads must avoid the resend.
    d = make_dispatcher()
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimedOut("Timed out")
        return None

    d.bot.send_message = AsyncMock(side_effect=flaky)
    ok = await d.send_text("hello")
    expect(
        "text send retries past a single timeout and ultimately succeeds",
        ok and calls["n"] == 2,
        f"calls={calls['n']}",
    )


async def test_retry_after_still_respected_for_uploads() -> None:
    # RetryAfter (rate limit) must still retry â€” only TimedOut is special-cased.
    d = make_dispatcher()
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryAfter(0)  # 0s so the test doesn't actually sleep long
        return None

    d.bot.send_document = AsyncMock(side_effect=flaky)
    ok = await d.send_photo(MEDIA)
    expect(
        "upload retries after a RetryAfter and then succeeds",
        ok and calls["n"] == 2,
        f"calls={calls['n']}",
    )


PARSE_ERROR = 'Can\'t parse entities: unsupported start tag "3" at byte offset 22'


async def test_unparseable_text_goes_out_as_plain_text() -> None:
    """A message whose markup Telegram rejects used to be resent unchanged
    four times and then lost. It now goes out once more, as plain text — and
    a literal "<3" in someone's name survives as text."""
    d = make_dispatcher()
    d.bot.send_message = AsyncMock(side_effect=[BadRequest(PARSE_ERROR), None])
    started = asyncio.get_running_loop().time()
    ok = await d.send_text("<b>@lis</b> renamed to Lisianna<3 &amp; co")
    elapsed = asyncio.get_running_loop().time() - started
    calls = d.bot.send_message.await_args_list
    expect("delivered on the second try", ok and len(calls) == 2, f"calls={len(calls)}")
    if len(calls) == 2:
        kw = calls[1].kwargs
        expect("the retry carries no parse mode", kw.get("parse_mode") is None, repr(kw))
        expect("and the words without the markup",
               kw.get("text") == "@lis renamed to Lisianna<3 & co", repr(kw.get("text")))
    expect("without the backoff", elapsed < 1.0, f"{elapsed:.2f}s")


async def test_unparseable_caption_goes_out_as_plain_text() -> None:
    d = make_dispatcher()
    d.bot.send_document = AsyncMock(side_effect=[BadRequest(PARSE_ERROR), None])
    ok = await d.send_document(MEDIA, caption="<b>@lis</b> — Lisianna<3")
    calls = d.bot.send_document.await_args_list
    expect("the upload is delivered on the second try",
           ok and len(calls) == 2, f"calls={len(calls)}")
    if len(calls) == 2:
        kw = calls[1].kwargs
        expect("with a plain caption",
               kw.get("parse_mode") is None and kw.get("caption") == "@lis — Lisianna<3",
               repr((kw.get("parse_mode"), kw.get("caption"))))


async def test_other_bad_requests_fail_at_once() -> None:
    """Telegram answers the same 400 to the same request: one try, no 24 s
    of backoff holding up the messages behind it."""
    d = make_dispatcher()
    d.bot.send_message = AsyncMock(side_effect=BadRequest("Chat not found"))
    started = asyncio.get_running_loop().time()
    ok = await d.send_text("hello")
    elapsed = asyncio.get_running_loop().time() - started
    expect("a rejected message reports failure",
           ok is False and d.bot.send_message.await_count == 1,
           f"ok={ok} calls={d.bot.send_message.await_count}")
    expect("straight away", elapsed < 1.0, f"{elapsed:.2f}s")

    plain = make_dispatcher()
    plain.bot.send_message = AsyncMock(side_effect=BadRequest(PARSE_ERROR))
    ok = await plain.send_text("<b>still</b> broken")
    expect("a plain-text retry that also fails stops there",
           ok is False and plain.bot.send_message.await_count == 2,
           f"ok={ok} calls={plain.bot.send_message.await_count}")


async def test_a_dead_topic_still_falls_back_to_general() -> None:
    d = make_dispatcher()
    d.bot.send_message = AsyncMock(
        side_effect=[BadRequest("Message thread not found"), None]
    )
    ok = await d.send_text("hi", message_thread_id=7)
    calls = d.bot.send_message.await_args_list
    expect("resent to General", ok and len(calls) == 2
           and calls[1].kwargs.get("message_thread_id") is None, repr(calls))
    if len(calls) == 2:
        expect("with its formatting kept",
               calls[1].kwargs.get("parse_mode") == "HTML", repr(calls[1].kwargs))


async def main() -> int:
    await test_upload_timeout_sends_once_no_retry()
    await test_video_timeout_sends_once()
    await test_document_timeout_sends_once()
    await test_upload_passes_generous_timeouts()
    await test_upload_success_path()
    await test_text_timeout_still_retries()
    await test_retry_after_still_respected_for_uploads()
    await test_unparseable_text_goes_out_as_plain_text()
    await test_unparseable_caption_goes_out_as_plain_text()
    await test_other_bad_requests_fail_at_once()
    await test_a_dead_topic_still_falls_back_to_general()

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.exit(asyncio.run(main()))
