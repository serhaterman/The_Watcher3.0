"""Who may read the HTTP API's monitoring data (2026-11-05).

Until that day `GET /accounts` answered anyone with the service URL with every
monitored username, its last check and its failure count, and `GET /status`
with the account counts and fetch health. Both now need WEB_API_TOKEN, and,
unlike the mutation endpoints' check, they fail CLOSED: a deployment without
a token publishes nothing.

What must hold:
- with no token configured, /status and /accounts are disabled (404);
- with one, a missing or wrong X-API-Token is a 401, never the data, and a
  header that is not ASCII is a 401 too, not a 500;
- the right token reads both, exactly as before;
- /health and /ready stay public, and /ready carries the deployed commit so
  a deploy can still be confirmed from outside.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
DB_FILE = ROOT / "test_api_auth.db"
if DB_FILE.exists():
    DB_FILE.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.api.routes import router  # noqa: E402
from app.config import settings  # noqa: E402
from app.database.models import Base, MonitoredAccount  # noqa: E402
from app.database.session import engine, get_session  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.monitor = object()
    app.state.scheduler = SimpleNamespace(
        scheduler=SimpleNamespace(running=True), next_run_time=None,
    )
    return app


async def test_monitoring_data_needs_the_token() -> None:
    old_token, old_commit = settings.web_api_token, settings.render_git_commit
    settings.render_git_commit = "9af3089"
    transport = httpx.ASGITransport(app=_app())
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://bot") as client:
            settings.web_api_token = None
            for path in ("/accounts", "/status"):
                r = await client.get(path)
                expect(f"no token configured: {path} is disabled",
                       r.status_code == 404, f"{r.status_code} {r.text[:80]}")
                r = await client.get(path, headers={"X-API-Token": "guess"})
                expect(f"no token configured: a guessed one does not open {path}",
                       r.status_code == 404, f"{r.status_code} {r.text[:80]}")

            settings.web_api_token = "sekrit"
            for path in ("/accounts", "/status"):
                r = await client.get(path)
                expect(f"{path} without the token is a 401",
                       r.status_code == 401, f"{r.status_code} {r.text[:80]}")
                r = await client.get(path, headers={"X-API-Token": "wrong"})
                expect(f"{path} with a wrong token is a 401",
                       r.status_code == 401, f"{r.status_code} {r.text[:80]}")
                r = await client.get(path, headers={"X-API-Token": b"s\xe9krit"})
                expect(f"{path} with a non-ASCII token is a 401, not a 500",
                       r.status_code == 401, f"{r.status_code} {r.text[:80]}")
                expect(f"{path} refusals leak no usernames",
                       "watched_one" not in r.text, r.text[:120])

            r = await client.get("/accounts", headers={"X-API-Token": "sekrit"})
            names = [a.get("username") for a in (r.json() or {}).get("accounts", [])]
            expect("the right token reads /accounts",
                   r.status_code == 200 and "watched_one" in names, f"{r.status_code} {names}")
            r = await client.get("/status", headers={"x-api-token": "sekrit"})
            expect("the right token reads /status, header name in any case",
                   r.status_code == 200 and r.json().get("accounts_total") == 1,
                   f"{r.status_code} {r.text[:120]}")
            expect("and /status still reports the commit",
                   r.status_code == 200 and r.json().get("commit") == "9af3089", r.text[:120])

            # The mutation endpoints share the comparison: a wrong or non-ASCII
            # token is refused there too, and never reaches the handler.
            for path in ("/sweep", "/accounts/watched_one/recheck"):
                for header in ("wrong", b"s\xe9krit"):
                    r = await client.post(path, headers={"X-API-Token": header})
                    expect(f"POST {path} with a bad token {header!r} is a 401",
                           r.status_code == 401, f"{r.status_code} {r.text[:80]}")

            old_home = settings.home_fetch_token
            settings.home_fetch_token = "phone-secret"
            try:
                r = await client.get("/home-fetch/jobs?wait=0",
                                     headers={"X-Watcher-Token": b"ph\xf6ne"})
                expect("the home fetcher refuses a non-ASCII token with a 401, not a 500",
                       r.status_code == 401, f"{r.status_code} {r.text[:80]}")
            finally:
                settings.home_fetch_token = old_home

            for token in (None, "sekrit"):
                settings.web_api_token = token
                r = await client.get("/health")
                expect(f"/health stays public (token {'set' if token else 'unset'})",
                       r.status_code == 200, f"{r.status_code}")
                r = await client.get("/ready")
                expect(f"/ready stays public and names the deployed commit "
                       f"(token {'set' if token else 'unset'})",
                       r.status_code == 200 and r.json().get("commit") == "9af3089",
                       f"{r.status_code} {r.text[:120]}")
    finally:
        settings.web_api_token, settings.render_git_commit = old_token, old_commit


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_session() as session:
        session.add(MonitoredAccount(username="watched_one", active=True))

    await test_monitoring_data_needs_the_token()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All API auth checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
