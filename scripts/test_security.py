"""The security review of 2026-11-05, as regression tests.

What must hold:
- a media folder is always directly inside MEDIA_DIR: "..", "." or a name
  with a path separator never names a folder outside it;
- a download link is fetched only over http(s) to a public host — never a
  file:// read, a loopback or private address in any form libcurl reads as
  one ("127.1", "2130706433", "%31%32%37.0.0.1"), or localhost — by both
  downloaders, before any request is made; every redirect hop is held to
  the same rule, and an answer from a non-public address (a public name that
  resolves inward) is dropped;
- a command never takes "." or ".." for a username, and a rename read off
  Instagram's data is stored only when it is a real handle;
- the Telegram webhook always has a secret, generated when none is set;
- without TELEGRAM_ADMIN_IDS only the chats the bot reports to may drive
  it, not anyone who finds it;
- the home fetcher's upload is bounded, raw and once inflated.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_security.db"
if DB_FILE.exists():
    DB_FILE.unlink()
MEDIA_DIR = tempfile.mkdtemp(prefix="watcher_security_media_")

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")
os.environ["MEDIA_DIR"] = MEDIA_DIR
for _proxy in ("PROXY_URL", "HTTPS_PROXY", "HTTP_PROXY"):
    os.environ.pop(_proxy, None)

import httpx  # noqa: E402
from curl_cffi.requests import AsyncSession  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.api.routes import router  # noqa: E402
from app.bot import handlers  # noqa: E402
from app.config import settings  # noqa: E402
from app.database.models import Base, MonitoredAccount  # noqa: E402
from app.database.session import dispose_engine, engine, get_session  # noqa: E402
from app.main import ensure_webhook_secret  # noqa: E402
from app.monitor import home_fetch  # noqa: E402
from app.monitor.media_hasher import MediaHasher  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402
from app.monitor.stories import StoriesClient, StoryItem  # noqa: E402
from app.utils import downloads  # noqa: E402
from app.utils.downloads import (  # noqa: E402
    RefusedURL,
    account_media_dir,
    fetch_public,
    unsafe_media_url,
)

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


# ---------- paths and URLs ----------------------------------------------------

def test_a_media_folder_never_leaves_the_media_root() -> None:
    root = settings.media_path.resolve()
    for hostile in ("..", ".", "...", "../etc", "a/b", "a\\b", "", "x" * 40, "ab\x00c"):
        folder = account_media_dir(hostile).resolve()
        expect(f"{hostile!r} stays directly inside the media root",
               folder.parent == root and folder.name not in ("", ".", ".."),
               str(folder))
    expect("a real handle keeps its own name",
           account_media_dir("Whos.Lisianna").name == "whos.lisianna")
    expect("the same odd name always maps to the same folder",
           account_media_dir("..") == account_media_dir(".."))


def test_only_public_http_links_are_fetched() -> None:
    refused = [
        "file:///etc/passwd", "file://C:/Windows/win.ini", "gopher://x/", "ftp://h/f",
        "dict://127.0.0.1:11211/", "http://127.0.0.1:8000/status", "http://localhost/",
        "https://LOCALHOST./x", "http://api.localhost/", "http://169.254.169.254/latest/",
        "http://10.0.0.5/", "http://192.168.1.1/", "http://[::1]/", "http://0.0.0.0/",
        "http://[fe80::1]/", "", "not a url", "https:///nohost",
        # Every form below reached a server on 127.0.0.1 through libcurl
        # (measured 2026-11-05) while the first version of the guard let it by.
        "http://2130706433/", "http://127.1/", "http://0x7f.0.0.1/", "http://0177.0.0.1/",
        "http://0x7f000001/", "http://127.000.000.001/", "http://%31%32%37.0.0.1/",
        "http://[::ffff:127.0.0.1]/", "http://10.1/", "http://0/",
        "http://ｌｏｃａｌｈｏｓｔ/",  # fullwidth "localhost"
        "http://１２７.0.0.1/",  # fullwidth digits
        "http://100.64.0.1/",  # shared address space, not public
    ]
    for url in refused:
        expect(f"refused: {url!r}", unsafe_media_url(url) is not None, repr(unsafe_media_url(url)))
    allowed = [
        "https://scontent-lga3-1.cdninstagram.com/v/t51.82787-19/1_n.jpg?oh=00&oe=1",
        "https://dl.snapcdn.app/saveinsta?token=abc.def.ghi",
        "http://instagram.fsaw1-1.fna.fbcdn.net/v/x.jpg",
        "https://8.8.8.8/x",
        "https://1e100.net/x",  # starts with a digit, still a name
        "https://123.example.com/x",  # one numeric label is not an address
        "https://xn--bcher-kva.example/x",  # an IDN, already in ASCII
    ]
    for url in allowed:
        expect(f"allowed: {url[:48]}", unsafe_media_url(url) is None, repr(unsafe_media_url(url)))


async def test_the_downloaders_refuse_before_asking() -> None:
    stories = StoriesClient()
    stories._session = SimpleNamespace(get=AsyncMock(), close=AsyncMock())
    item = StoryItem(pk="evil1", taken_at=0, media_type="image",
                     url="file:///etc/passwd", source="story")
    got = await stories.download(item, "someone")
    expect("a story link to a local file is not downloaded",
           got is None and stories._session.get.await_count == 0, repr(got))

    hasher = MediaHasher()
    await hasher._client.close()
    hasher._client = SimpleNamespace(get=AsyncMock(), close=AsyncMock())
    got = await hasher._fetch_and_store("http://169.254.169.254/latest/meta-data", "someone")
    expect("an avatar link to a private address is not downloaded",
           got is None and hasher._client.get.await_count == 0, repr(got))


class _Answer:
    """One answer of a fake curl session: its status, the address libcurl
    connected to, and the redirect target libcurl reads off Location."""

    def __init__(self, status: int = 200, ip: str = "157.240.1.174",
                 to: str | None = None, content: bytes = b"\xff\xd8media") -> None:
        self.status_code, self.primary_ip, self.redirect_url = status, ip, to
        self.content, self.headers = content, {"Content-Type": "image/jpeg"}


class _FakeCurl:
    def __init__(self, *answers: _Answer) -> None:
        self.answers, self.asked = list(answers), []

    async def get(self, url: str, **kwargs) -> _Answer:
        self.asked.append((url, kwargs.get("allow_redirects")))
        return self.answers.pop(0)

    async def close(self) -> None:
        pass


async def _refusal(session: _FakeCurl, url: str) -> str | None:
    try:
        await fetch_public(session, url)
    except RefusedURL as exc:
        return str(exc)
    return None


async def test_every_hop_and_every_answer_is_checked() -> None:
    start = "https://dl.snapcdn.app/get?token=t"
    cdn = "https://scontent-lga3-1.cdninstagram.com/v/x.jpg"

    session = _FakeCurl(_Answer())
    got = await fetch_public(session, start)
    expect("a public answer comes back as is",
           got.content == b"\xff\xd8media" and session.asked == [(start, False)],
           repr(session.asked))

    session = _FakeCurl(_Answer(302, to=cdn), _Answer())
    got = await fetch_public(session, start)
    expect("a redirect to a public host is followed, by the guard and not libcurl",
           session.asked == [(start, False), (cdn, False)], repr(session.asked))

    for target in ("http://127.0.0.1:8000/status", "file:///etc/passwd",
                   "http://2130706433/", "http://169.254.169.254/latest/meta-data"):
        session = _FakeCurl(_Answer(302, to=target), _Answer())
        why = await _refusal(session, start)
        expect(f"a redirect to {target} is refused before it is asked",
               why is not None and len(session.asked) == 1, f"{why!r} {session.asked}")

    session = _FakeCurl(_Answer(ip="127.0.0.1"))
    why = await _refusal(session, "http://127.0.0.1.nip.io:8000/status")
    expect("a public name that resolved to loopback is dropped",
           why is not None and "127.0.0.1" in why, repr(why))

    session = _FakeCurl(_Answer(302, ip="10.0.0.5", to=cdn), _Answer())
    why = await _refusal(session, start)
    expect("a redirect from a private address is not followed",
           why is not None and len(session.asked) == 1, f"{why!r} {session.asked}")

    session = _FakeCurl(_Answer(ip=""))
    why = await _refusal(session, start)
    expect("an answer with no connection address is dropped", why is not None, repr(why))

    loop = [_Answer(302, to=f"{cdn}?hop={n}") for n in range(40)]
    session = _FakeCurl(*loop)
    why = await _refusal(session, start)
    expect("a redirect loop ends at the limit",
           why is not None and len(session.asked) == downloads._MAX_REDIRECTS + 1,
           f"{why!r} after {len(session.asked)}")

    session = _FakeCurl(_Answer(304))
    got = await fetch_public(session, start)
    expect("a 3xx with no Location comes back for the caller to judge",
           got.status_code == 304 and len(session.asked) == 1)

    old_proxy = settings.proxy_url
    try:
        settings.proxy_url = "socks5h://127.0.0.1:1080"
        session = _FakeCurl(_Answer(ip="127.0.0.1"))
        got = await fetch_public(session, start)
        expect("through a configured proxy, the proxy's own address is no refusal",
               got.status_code == 200)
    finally:
        settings.proxy_url = old_proxy

    stories = StoriesClient()
    await stories._session.close()
    stories._session = _FakeCurl(_Answer(302, to=cdn), _Answer(content=b"story-bytes"))
    item = StoryItem(pk="3561234567", taken_at=0, media_type="image", url=start, source="story")
    path = await stories.download(item, "someone")
    expect("a story behind a redirect still downloads",
           path is not None and path.read_bytes() == b"story-bytes", repr(path))

    stories._session = _FakeCurl(_Answer(302, to="http://192.168.1.1/admin"), _Answer())
    item = StoryItem(pk="3561234568", taken_at=0, media_type="image", url=start, source="story")
    path = await stories.download(item, "someone")
    expect("a story redirected into the local network is not",
           path is None and len(stories._session.asked) == 1
           and not (account_media_dir("someone") / "stories" / "3561234568.jpg").exists(),
           repr(path))

    hasher = MediaHasher()
    await hasher._client.close()
    hasher._client = _FakeCurl(_Answer(ip="192.168.1.1"))
    got = await hasher._fetch_and_store(cdn, "someone")
    expect("an avatar answered from a private address is not stored", got is None, repr(got))


class _Local(BaseHTTPRequestHandler):
    """A server on 127.0.0.1 that counts what reaches it."""

    hits: list = []

    def do_GET(self) -> None:
        _Local.hits.append(self.path)
        if self.path == "/hop":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "8")
        self.end_headers()
        self.wfile.write(b"INTERNAL")

    def log_message(self, *args) -> None:
        pass


async def test_real_libcurl_never_reaches_this_machine() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Local)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    try:
        async with AsyncSession(impersonate="chrome120", timeout=5) as session:
            # What fetch_public relies on, from libcurl itself: the address
            # it connected to, and Location resolved without following it.
            raw = await session.get(f"http://127.0.0.1:{port}/hop", allow_redirects=False)
            expect("libcurl reports the address it connected to",
                   raw.primary_ip == "127.0.0.1", repr(raw.primary_ip))
            expect("and where a redirect leads, without going there",
                   raw.status_code == 302
                   and raw.redirect_url == f"http://127.0.0.1:{port}/final"
                   and _Local.hits == ["/hop"], f"{raw.redirect_url!r} {_Local.hits}")
            expect("an answer from it would be dropped",
                   downloads._unsafe_peer(raw) is not None)
            _Local.hits.clear()
            for host in ("2130706433", "127.1", "0x7f.0.0.1", "%31%32%37.0.0.1",
                         "127.0.0.1", "localhost", "[::ffff:127.0.0.1]"):
                try:
                    await fetch_public(session, f"http://{host}:{port}/secret")
                    why = None
                except RefusedURL as exc:
                    why = str(exc)
                expect(f"http://{host}:{port}/ is refused", why is not None, repr(why))
            expect("and nothing reached the local server", _Local.hits == [], repr(_Local.hits))
    finally:
        server.shutdown()
        server.server_close()


# ---------- usernames -----------------------------------------------------------

def test_a_command_never_takes_dots_for_a_username() -> None:
    for raw in (".", "..", "...", "@..", "../x", "a/b", ""):
        expect(f"{raw!r} is not a username", handlers._normalize_username(raw) is None)
    for raw in ("a.b", "_x_", "x.", ".x", "65xim", "@Whos.Lisianna"):
        expect(f"{raw!r} still is", handlers._normalize_username(raw) is not None)
    expect("/add .. is refused too", handlers._parse_add_target("..") == (None, None))


async def test_a_hostile_rename_is_not_stored() -> None:
    async with get_session() as session:
        account = MonitoredAccount(username="honest_name", active=True, instagram_id="7")
        session.add(account)
        await session.flush()
        account_id = account.id
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    service = MonitorService(instagram=SimpleNamespace(), hasher=AsyncMock(),
                             notifier=notifier, stories=None)
    for hostile in ("../../etc", "..", "a/b", "x" * 31, "spaces here"):
        kept = await service._apply_rename(account_id, "honest_name", hostile)
        expect(f"a rename to {hostile!r} is ignored", kept == "honest_name", kept)
    async with get_session() as session:
        stored = (await session.get(MonitoredAccount, account_id)).username
    expect("and the stored name is unchanged", stored == "honest_name", stored)
    expect("with no rename announced", notifier.send_text.await_count == 0,
           repr(notifier.send_text.await_args_list))
    real = await service._apply_rename(account_id, "honest_name", "Renamed.For_Real")
    expect("a real rename still goes through", real == "renamed.for_real", real)


# ---------- Telegram ------------------------------------------------------------

def test_the_webhook_always_has_a_secret() -> None:
    old = settings.telegram_webhook_secret
    try:
        settings.telegram_webhook_secret = None
        ensure_webhook_secret()
        made = settings.telegram_webhook_secret or ""
        expect("a missing secret is generated",
               len(made) >= 32 and all(c.isalnum() or c in "-_" for c in made), repr(made))
        settings.telegram_webhook_secret = "configured_secret"
        ensure_webhook_secret()
        expect("a configured one is kept",
               settings.telegram_webhook_secret == "configured_secret")
    finally:
        settings.telegram_webhook_secret = old


async def _refusal_text(*, chat: int, user: int, kind: str) -> str:
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat, type=kind),
                             effective_user=SimpleNamespace(id=user),
                             callback_query=None, message=message)
    rejected = await handlers._reject_if_unauthorized(update)
    return message.reply_text.await_args.args[0] if rejected else "(let in)"


async def test_only_the_reporting_chats_drive_the_bot_by_default() -> None:
    def update(chat: int, user: int):
        return SimpleNamespace(effective_chat=SimpleNamespace(id=chat),
                               effective_user=SimpleNamespace(id=user))

    old = (settings.telegram_admin_ids, settings.telegram_chat_id,
           settings.telegram_mirror_chat_ids)
    try:
        settings.telegram_admin_ids = ""
        settings.telegram_chat_id = "-1001234"
        settings.telegram_mirror_chat_ids = "555, @somechannel"
        expect("the notification chat may", handlers._is_authorized(update(-1001234, 42)))
        expect("so may a mirror chat", handlers._is_authorized(update(555, 42)))
        expect("a stranger's own chat may not",
               not handlers._is_authorized(update(777, 777)))
        expect("the access line names the default",
               "TELEGRAM_ADMIN_IDS" in handlers.describe_access())
        refused = await _refusal_text(chat=777, user=777, kind="private")
        expect("a refused DM says how its owner gets in",
               "777" in refused and "TELEGRAM_ADMIN_IDS" in refused, refused)
        refused = await _refusal_text(chat=-100999, user=777, kind="supergroup")
        expect("a refused group hears no more than 'Unauthorized.'",
               refused == "Unauthorized.", refused)
        settings.telegram_admin_ids = "777"
        expect("TELEGRAM_ADMIN_IDS still names who may",
               handlers._is_authorized(update(777, 777)))
        expect("and then only them",
               not handlers._is_authorized(update(-1001234, 42)))
        refused = await _refusal_text(chat=888, user=888, kind="private")
        expect("with a list set, a refused DM gets no hint",
               refused == "Unauthorized.", refused)
    finally:
        (settings.telegram_admin_ids, settings.telegram_chat_id,
         settings.telegram_mirror_chat_ids) = old


# ---------- the home fetcher's upload ---------------------------------------------

async def test_the_upload_is_bounded() -> None:
    from app.api import routes

    app = FastAPI()
    app.include_router(router)
    old_token, old_broker = settings.home_fetch_token, home_fetch.broker
    settings.home_fetch_token = "phone-secret"
    home_fetch.broker = home_fetch.HomeFetchBroker()
    headers = {"X-Watcher-Token": "phone-secret", "X-IG-Status": "200"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://bot") as client:
            r = await client.post("/home-fetch/jobs/nojob", headers=headers,
                                  content=b"<html>ok</html>")
            expect("a normal upload is read", r.status_code == 200, f"{r.status_code} {r.text}")
            small = gzip.compress(b"<html>" + b"a" * 1000 + b"</html>")
            r = await client.post("/home-fetch/jobs/nojob", content=small,
                                  headers={**headers, "Content-Encoding": "gzip"})
            expect("a normal gzip upload is read", r.status_code == 200, f"{r.status_code} {r.text}")
            big = b"x" * (routes._MAX_UPLOAD_BYTES + 1)
            r = await client.post("/home-fetch/jobs/nojob", headers=headers, content=big)
            expect("an oversized upload is refused with 413", r.status_code == 413, repr(r.status_code))
            bomb = gzip.compress(b"\0" * (routes._MAX_UPLOAD_BYTES * 3))
            r = await client.post("/home-fetch/jobs/nojob", content=bomb,
                                  headers={**headers, "Content-Encoding": "gzip"})
            expect(f"a {len(bomb) // 1024} KB gzip bomb is refused with 413",
                   r.status_code == 413, repr(r.status_code))
            r = await client.post("/home-fetch/jobs/nojob", content=b"not gzip",
                                  headers={**headers, "Content-Encoding": "gzip"})
            expect("a body that is not gzip is a 400", r.status_code == 400, repr(r.status_code))
            r = await client.post("/home-fetch/jobs/nojob", content=small[:-6],
                                  headers={**headers, "Content-Encoding": "gzip"})
            expect("a gzip body cut short is a 400", r.status_code == 400, repr(r.status_code))
            r = await client.post("/home-fetch/jobs/nojob", content=small + b"junk",
                                  headers={**headers, "Content-Encoding": "gzip"})
            expect("a gzip body with more after its end is a 400",
                   r.status_code == 400, repr(r.status_code))
    finally:
        settings.home_fetch_token, home_fetch.broker = old_token, old_broker


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_a_media_folder_never_leaves_the_media_root()
    test_only_public_http_links_are_fetched()
    await test_the_downloaders_refuse_before_asking()
    await test_every_hop_and_every_answer_is_checked()
    await test_real_libcurl_never_reaches_this_machine()
    test_a_command_never_takes_dots_for_a_username()
    await test_a_hostile_rename_is_not_stored()
    test_the_webhook_always_has_a_secret()
    await test_only_the_reporting_chats_drive_the_bot_by_default()
    await test_the_upload_is_bounded()

    await dispose_engine()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All security checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
