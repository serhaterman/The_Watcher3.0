"""FastAPI application entrypoint.

Wires together the database, Instagram client, media hasher, Telegram bot,
notification dispatcher, change detection engine, and APScheduler-driven worker.
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from telegram.ext import Application as TgApplication

from app.api.routes import router as api_router
from app.bot.handlers import (
    BOT_COMMANDS,
    PANEL_CHAT_ID,
    PANEL_MSG_ID,
    describe_access,
    instagram_route,
    register_handlers,
)
from app.bot.notifications import build_dispatcher
from app.bot.panel_bump import PanelBumper
from app.config import settings
from app.database import crud
from app.database.session import dispose_engine, get_session, init_db
from app.monitor import page_tunnel
from app.monitor.instagram import InstagramClient
from app.monitor.media_hasher import MediaHasher
from app.monitor.service import MonitorService
from app.monitor.stories import StoriesClient
from app.utils.logger import logger, quiet_health_check_logs
from app.workers.scheduler import WatcherScheduler


def ensure_webhook_secret() -> None:
    """Give the webhook a secret when none is configured.

    Without one the webhook took ANY post as Telegram's — and an update says
    which user sent it, admins included. A random secret for this run is
    registered with setWebhook and checked on every post (see
    routes.telegram_webhook). Render generates TELEGRAM_WEBHOOK_SECRET
    (render.yaml); this covers every other deployment."""
    if settings.telegram_webhook_secret:
        return
    settings.telegram_webhook_secret = secrets.token_urlsafe(32)
    logger.warning(
        "TELEGRAM_WEBHOOK_SECRET is not set — using a random one for this run, "
        "so the webhook only accepts Telegram's own posts"
    )


# The page-tunnel exit Instagram last served pages through. A restart starts
# on it, not on the first config in the file (PageTunnel.report_answered).
TUNNEL_EXIT_SETTING = "page_tunnel_exit"


async def remembered_tunnel_exit() -> Optional[str]:
    async with get_session() as session:
        return await crud.get_setting(session, TUNNEL_EXIT_SETTING) or None


async def remember_tunnel_exit(label: str) -> None:
    async with get_session() as session:
        await crud.set_setting(session, TUNNEL_EXIT_SETTING, label)
    logger.info("Page tunnel: Instagram serves pages through {} — a restart starts there", label)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    quiet_health_check_logs()
    logger.info("Starting The Watcher V3.0…")
    await init_db()
    # The page tunnel next: it decides where this host's profile-page
    # requests leave from, and the route line below should say so. It starts
    # on the exit Instagram last served pages through — measured 2026-11-05,
    # starting on the first config after a deploy cost a whole sweep on an
    # exit Instagram had stopped serving.
    await page_tunnel.tunnel.start(prefer=await remembered_tunnel_exit())
    page_tunnel.tunnel.remember_exits(remember_tunnel_exit)
    # Say the route once, at boot. A cloud host's own IP is 401-blocked, so a
    # misconfigured IG_PROXY_URL looks exactly like Instagram blocking us.
    logger.info("Instagram requests route via: {}", instagram_route())

    # One-time cleanup: the first public-page parser read the og: meta block,
    # whose counts were verified wrong against the real profile. The card reads
    # the newest snapshot, so those rows have to go or it keeps stating a wrong
    # number as current. Bounded to rows written before the payload parser
    # shipped (crud.OG_ERA_CUTOFF), so it never touches the good page-sourced
    # rows the current fallback writes. A no-op on every boot after the first.
    async with get_session() as session:
        purged = await crud.purge_partial_snapshots(session)
    if purged:
        logger.warning(
            "Removed {} snapshot(s) from the withdrawn og: page source — "
            "their follower/following counts did not match Instagram", purged,
        )

    # Telegram bot
    tg_app = (
        TgApplication.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )
    register_handlers(tg_app)
    logger.info("Telegram access: {}", describe_access())

    # Build core services
    instagram = InstagramClient()
    hasher = MediaHasher()
    stories = StoriesClient()
    dispatcher = build_dispatcher(tg_app.bot)
    monitor = MonitorService(instagram, hasher, dispatcher, stories)
    scheduler = WatcherScheduler(monitor)

    # Cross-wire so /status can read scheduler info & handlers can run checks
    tg_app.bot_data["monitor"] = monitor
    tg_app.bot_data["scheduler"] = scheduler

    def _state_change(state: str, next_run) -> None:
        tg_app.bot_data["scheduler_state"] = state
        tg_app.bot_data["next_run"] = next_run

    scheduler.set_state_callback(_state_change)

    # --- Panel-bump: keep the main-menu message at the bottom of the chat ---
    # Load persisted panel position from DB so it survives server restarts.
    async with get_session() as _s:
        _saved_mid = await crud.get_setting(_s, "panel_msg_id")
        _saved_cid = await crud.get_setting(_s, "panel_chat_id")
    if _saved_mid and _saved_cid:
        tg_app.bot_data[PANEL_MSG_ID] = int(_saved_mid)
        tg_app.bot_data[PANEL_CHAT_ID] = int(_saved_cid)

    async def _persist_panel(msg_id: int, chat_id: int) -> None:
        async with get_session() as session:
            await crud.set_setting(session, "panel_msg_id", str(msg_id))
            await crud.set_setting(session, "panel_chat_id", str(chat_id))

    # The bump keeps the menu at the bottom after automated sweep notifications,
    # but skips while a manual download is running so it never drops a duplicate
    # menu under an on-demand result (see PanelBumper).
    panel_bumper = PanelBumper(
        tg_app.bot,
        tg_app.bot_data,
        download_active=lambda: monitor.download_active,
        persist=_persist_panel,
    )
    dispatcher.post_send_hook = panel_bumper.schedule

    # Save on app state for HTTP API access
    app.state.monitor = monitor
    app.state.scheduler = scheduler
    app.state.tg_app = tg_app
    app.state.instagram = instagram
    app.state.hasher = hasher

    # Start the Telegram Application. In webhook mode we never start the
    # updater — incoming updates are pushed into `tg_app.update_queue` by
    # the FastAPI webhook endpoint. In polling mode we run the updater.
    await tg_app.initialize()
    await tg_app.start()
    try:
        await tg_app.bot.set_my_commands(BOT_COMMANDS)
    except Exception as exc:
        logger.warning("Failed to set bot commands menu: {}", exc)

    allowed_updates = ["message", "edited_message", "callback_query"]

    if settings.telegram_use_webhook:
        webhook_url = settings.telegram_webhook_full_url
        ensure_webhook_secret()
        # Drop the webhook first so any prior URL/secret is cleared, then
        # register ours. `drop_pending_updates` discards the backlog that
        # built up while polling was running, preventing a flood at switchover.
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
        except Exception as exc:
            logger.warning("Failed to clear prior webhook: {}", exc)
        await tg_app.bot.set_webhook(
            url=webhook_url,
            secret_token=settings.telegram_webhook_secret or None,
            allowed_updates=allowed_updates,
            drop_pending_updates=True,
        )
        logger.info("Telegram webhook registered: {}", webhook_url)
    else:
        # Local dev fallback — long-polling. Only one consumer per token works,
        # so do not run this alongside a deployed webhook.
        if tg_app.updater:
            await tg_app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=allowed_updates,
            )
            logger.info("Telegram long-polling started (no webhook URL configured)")

    await scheduler.start()
    logger.info("The Watcher is online.")

    try:
        yield
    finally:
        logger.info("Shutting down…")
        try:
            await scheduler.shutdown()
        except Exception as exc:
            logger.warning("Scheduler shutdown error: {}", exc)

        try:
            if settings.telegram_use_webhook:
                # Leave the webhook registered on shutdown — Render's rolling
                # deploys overlap, so deleting here would briefly break the
                # incoming side for the new instance. The new instance will
                # re-register on startup with the same URL/secret.
                pass
            elif tg_app.updater and tg_app.updater.running:
                await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception as exc:
            logger.warning("Telegram shutdown error: {}", exc)

        try:
            await instagram.close()
            await hasher.close()
            await stories.close()
        except Exception as exc:
            logger.warning("HTTP client shutdown error: {}", exc)

        try:
            await page_tunnel.tunnel.stop()
        except Exception as exc:
            logger.warning("Page tunnel shutdown error: {}", exc)

        await dispose_engine()
        logger.info("Shutdown complete.")


app = FastAPI(
    title="The Watcher V3.0",
    description="Instagram account intelligence monitoring platform.",
    version="3.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "name": "The Watcher V3.0",
            "status": "running",
            "endpoints": [
                "/health",
                "/ready",
                "/status",
                "/accounts",
                "/accounts/{username}/recheck",
                "/sweep",
                "/home-fetch/jobs",
            ],
        }
    )


app.include_router(api_router)
