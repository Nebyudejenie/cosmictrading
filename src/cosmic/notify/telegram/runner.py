"""Telegram bot process: outbound long polling (no webhook) + outbox delivery (ADR-016)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from decimal import Decimal, InvalidOperation
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select, update

from cosmic.core.clock import SystemClock
from cosmic.core.config import Settings
from cosmic.core.errors import ConfigError
from cosmic.core.logs import get_logger
from cosmic.core.redaction import scrub_text
from cosmic.db.engine import Database
from cosmic.db.schema import notifications, users
from cosmic.notify.alerts import FIELDS, LABELS, alert_from_payload, alert_headline
from cosmic.notify.telegram.handlers import Reply, TelegramHandlers, Update
from cosmic.ops.heartbeat import beat
from cosmic.trading.services import TradingServices

log = get_logger(__name__)
OUTBOX_INTERVAL = 5


def _markup(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in reply.buttons
        ]
    )


def notification_text(kind: str, payload: dict[str, Any]) -> str:
    alert = alert_from_payload(payload)
    if alert is not None:  # every alert answers the same six questions (TRD-33)
        head = f"{alert['severity']}: {alert_headline(alert)}"
        return "\n".join([head] + [f"{LABELS[f]}: {alert[f]}" for f in FIELDS])
    if kind in ("daily_report", "weekly_report", "monthly_report"):
        return str(payload.get("text") or "")
    # Newer payloads name the account with its environment (DIR-02); older ones only say whether
    # they were simulated.
    ref = payload.get("account_ref")
    on = f" on {ref}" if ref else ""
    sim = "" if ref else (" [SIMULATED]" if payload.get("simulated") else "")
    if kind == "order_fill":
        return (
            f"Order {payload.get('client_order_id')}{on}: {payload.get('status')} "
            f"{payload.get('filled_qty')} @ {payload.get('avg_price')}{sim} (broker-confirmed)"
        )
    if kind in ("order_acknowledged", "order_rejected"):
        order, status = payload.get("client_order_id"), payload.get("status")
        why = f" {payload['reason']}" if payload.get("reason") else ""
        return f"Order {order}{on}: {status}{sim}.{why}"
    if kind == "trade_closed":
        pnl = payload.get("realized_pnl")
        if payload.get("instrument"):  # newer payloads: say plainly whether it won
            try:
                value = Decimal(str(pnl))
            except (InvalidOperation, ValueError):
                value = Decimal(0)
            outcome = "WIN" if value > 0 else ("LOSS" if value < 0 else "EVEN")
            mark = {"WIN": "✅", "LOSS": "❌", "EVEN": "➖"}[outcome]
            auto = "auto-trade " if payload.get("auto") else "trade "
            return (
                f"{mark} {outcome} {value:+,.2f} {payload.get('currency', '')}: "
                f"{payload['instrument']} {auto}{payload.get('trade_code')}{on} closed "
                "(after fees)."
            )
        return f"{payload.get('trade_code')}{on} closed. Realised P&L after fees: {pnl}{sim}"
    if kind == "auto_trade_soon":
        return str(payload.get("text") or "")
    if kind == "mandate_changed":
        return str(payload.get("text") or "")
    if kind == "auto_trade":
        return (
            f"AUTO TRADE {payload.get('trade_code')}{on}: {payload.get('action')}. Loss at the "
            f"stop at most {payload.get('max_loss')}. Rule {payload.get('rule')} on "
            f"{payload.get('bars') or '1-hour bars'}, "
            "approved under your demo mandate (a forward test, not a proven edge)."
        )
    if kind == "auto_trade_skipped":
        code = f" {payload['trade_code']}" if payload.get("trade_code") else ""
        return (
            f"Auto-trader skipped{code}{on}: {payload.get('action')}. Why: {payload.get('reason')}"
        )
    if kind == "proposal_expired":
        return f"Proposal {payload.get('trade_code', '')}{on} expired without a decision."
    return f"{kind}: {payload.get('trade_code', '')}{on}"


async def deliver_outbox(bot: Bot, db: Database, handlers: TelegramHandlers) -> int:
    sent = 0
    async with db.connect() as conn:
        rows = list(
            await conn.execute(
                select(notifications, users.c.telegram_user_id)
                .join(users, users.c.id == notifications.c.user_id)
                .where(notifications.c.channel == "TELEGRAM", notifications.c.status == "PENDING")
                .order_by(notifications.c.created_at)
                .limit(20)
            )
        )
    for n in rows:
        try:
            if n.telegram_user_id is None:
                raise RuntimeError("user is no longer linked")
            if n.kind == "approval_requested":
                import uuid as _uuid

                reply = await handlers._proposal_message(
                    n.telegram_user_id, _uuid.UUID(n.payload["intent_id"])
                )
            else:
                reply = Reply(n.telegram_user_id, notification_text(n.kind, n.payload))
            await bot.send_message(reply.chat_id, reply.text, reply_markup=_markup(reply))
            values: dict[str, Any] = {"status": "SENT", "sent_at": SystemClock().now()}
            sent += 1
        except Exception as exc:  # delivery failures are recorded, never hidden
            values = {
                "status": "FAILED" if n.attempts >= 4 else "PENDING",
                "last_error": scrub_text(str(exc))[:500],
            }
        async with db.transaction() as conn:
            await conn.execute(
                update(notifications)
                .where(notifications.c.id == n.id)
                .values(attempts=notifications.c.attempts + 1, **values)
            )
    return sent


async def run_bot(settings: Settings) -> int:
    if settings.telegram_bot_token is None:
        raise ConfigError(
            "COSMIC_TELEGRAM_BOT_TOKEN is not configured; Telegram stays disabled (B-05)"
        )
    db = Database.from_settings(settings)
    clock = SystemClock()
    handlers = TelegramHandlers(db, clock, settings, TradingServices.build(db, clock, settings))
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    dp = Dispatcher()

    @dp.message()
    async def on_message(message: Message) -> None:
        if message.chat.type != "private":  # approvals happen only in private chats
            with contextlib.suppress(Exception):
                await bot.leave_chat(message.chat.id)  # never stay in groups or channels
            return
        if message.from_user is None:
            return
        upd = Update(
            message.from_user.id, message.chat.id, message.chat.type, text=message.text or ""
        )
        for r in await handlers.handle(upd):
            await bot.send_message(r.chat_id, r.text, reply_markup=_markup(r))

    @dp.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        await query.answer()
        if query.message is None:
            return
        upd = Update(
            query.from_user.id,
            query.message.chat.id,
            query.message.chat.type,
            callback_data=query.data or "",
        )
        for r in await handlers.handle(upd):
            await bot.send_message(r.chat_id, r.text, reply_markup=_markup(r))

    bot_id = f"{socket.gethostname()}-{os.getpid()}-bot"
    started = clock.now()

    async def outbox_loop() -> None:
        while True:
            try:
                await deliver_outbox(bot, db, handlers)
                async with db.transaction() as conn:  # liveness + "Telegram is configured"
                    await beat(
                        conn,
                        worker_id=bot_id,
                        process="bot",
                        started_at=started,
                        now=clock.now(),
                        info={"integrations": {"telegram": "CONFIGURED"}},
                    )
                if settings.heartbeat_file is not None:  # container healthcheck
                    try:
                        settings.heartbeat_file.touch()
                    except OSError:
                        log.warning("heartbeat_file_unwritable")
            except Exception as exc:
                log.warning("outbox_error", error=type(exc).__name__)
            await asyncio.sleep(OUTBOX_INTERVAL)

    task = asyncio.create_task(outbox_loop())
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await db.dispose()
    return 0
