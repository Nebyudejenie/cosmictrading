"""Telegram update handling (V6 §1823). Pure logic over domain services; no aiogram here.

* Private chats only; the sender's immutable user ID must be linked to an active user.
* Buttons carry opaque references (intent IDs, challenge IDs) — never credentials.
* Approval = APPROVE EXACT TRADE → confirmation screen → CONFIRM → (TOTP code, default on).
  Free text such as "ok" or "looks good" is never treated as approval.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cosmic.audit.service import Actor
from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.core.errors import CosmicError
from cosmic.db.engine import Database
from cosmic.identity.capabilities import Capability
from cosmic.identity.service import Principal, verify_totp_for_user
from cosmic.notify.report import health_lines
from cosmic.notify.telegram import format as fmt
from cosmic.notify.telegram.linking import link, principal_for_telegram
from cosmic.trading import approvals, queries
from cosmic.trading.proposals import get_intent, latest_packet
from cosmic.trading.services import TradingServices

Button = tuple[str, str]
PENDING_TTL = timedelta(minutes=3)
_CODE = re.compile(r"^\d{6}$")


@dataclass(frozen=True)
class Update:
    user_id: int
    chat_id: int
    chat_type: str  # private | group | supergroup | channel
    text: str | None = None
    callback_data: str | None = None


@dataclass(frozen=True)
class Reply:
    chat_id: int
    text: str
    buttons: list[list[Button]] = field(default_factory=list)


@dataclass
class _Pending:
    kind: str  # "approve" | "stop"
    ref: str
    packet_hash: str
    expires_at: datetime


class TelegramHandlers:
    def __init__(
        self, db: Database, clock: Clock, settings: Settings, trading: TradingServices
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.trading = trading
        self._pending: dict[int, _Pending] = {}

    async def handle(self, u: Update) -> list[Reply]:
        if u.chat_type != "private":
            return []  # never act on group/channel messages
        text = (u.text or "").strip()
        if text.startswith("/link"):
            return [await self._link(u, text)]
        async with self.db.connect() as conn:
            principal = await principal_for_telegram(conn, u.user_id)
        if principal is None:
            return [
                Reply(
                    u.chat_id,
                    "This bot only serves its linked operator. Link your account from the portal.",
                )
            ]
        try:
            if u.callback_data:
                return await self._callback(u, principal, u.callback_data)
            return await self._text(u, principal, text)
        except CosmicError as exc:
            return [Reply(u.chat_id, f"Not done: {exc.message}")]

    # ---- text ---------------------------------------------------------------------------

    async def _link(self, u: Update, text: str) -> Reply:
        parts = text.split()
        if len(parts) != 2:
            return Reply(u.chat_id, "Usage: /link CODE (create the code in the portal)")
        try:
            async with self.db.transaction() as conn:
                await link(conn, code=parts[1], telegram_user_id=u.user_id, now=self.clock.now())
        except CosmicError as exc:
            return Reply(u.chat_id, f"Not linked: {exc.message}")
        return Reply(u.chat_id, "Linked. You will receive proposals and alerts here.")

    async def _text(self, u: Update, p: Principal, text: str) -> list[Reply]:
        if text in ("/start", "/help"):
            return [
                Reply(
                    u.chat_id,
                    "Commands: /status, /approvals, /stop <reason>. Approvals always need the "
                    "APPROVE EXACT TRADE button, a confirmation and your authenticator code.",
                )
            ]
        if text == "/status":
            async with self.db.connect() as conn:
                lines = await health_lines(conn, self.clock.now())
            return [Reply(u.chat_id, "\n".join(lines))]
        if text == "/approvals":
            async with self.db.connect() as conn:
                waiting = await queries.intent_list(conn, statuses=("PROPOSED",), limit=5)
            if not waiting:
                return [Reply(u.chat_id, "No proposals are waiting.")]
            return [await self._proposal_message(u.chat_id, i["id"]) for i in waiting]
        if text.startswith("/stop"):
            reason = text[5:].strip()
            if len(reason) < 3:
                return [Reply(u.chat_id, "Usage: /stop <reason>")]
            if not p.has(Capability.ACTIVATE_EMERGENCY_STOP):
                return [Reply(u.chat_id, "You are not allowed to activate the emergency stop.")]
            self._pending[u.user_id] = _Pending(
                "stop", reason[:200], "", self.clock.now() + PENDING_TTL
            )
            return [
                Reply(
                    u.chat_id,
                    f"Emergency stop: block all new exposure. Existing positions are NOT closed.\n"
                    f"Reason: {reason[:200]}",
                    [[("CONFIRM EMERGENCY STOP", "k")]],
                )
            ]
        pending = self._pending.get(u.user_id)
        if pending and pending.kind == "approve" and _CODE.match(text):
            return [await self._finish_approval(u, p, pending, text)]
        return [
            Reply(u.chat_id, "I only act on buttons and commands. Free text is never an approval.")
        ]

    # ---- callbacks ----------------------------------------------------------------------

    async def _callback(self, u: Update, p: Principal, data: str) -> list[Reply]:
        kind, _, ref = data.partition(":")
        if kind == "v":  # view section: v:<intent>:<section>
            intent_ref, _, name = ref.partition(":")
            async with self.db.connect() as conn:
                packet = await latest_packet(conn, uuid.UUID(intent_ref))
            return [Reply(u.chat_id, fmt.section(packet.packet, name))]
        if kind in ("a", "r", "rc"):
            return [await self._start_decision(u, p, uuid.UUID(ref), kind)]
        if kind == "c":
            pending = self._pending.get(u.user_id)
            if (
                pending is None
                or pending.kind != "approve"
                or pending.ref != ref
                or pending.expires_at <= self.clock.now()
            ):
                return [
                    Reply(
                        u.chat_id, "This confirmation is no longer valid. Review the trade again."
                    )
                ]
            if self.settings.telegram_approval_requires_totp:
                return [
                    Reply(
                        u.chat_id,
                        "Send your 6-digit authenticator code to approve this exact trade.",
                    )
                ]
            return [await self._finish_approval(u, p, pending, None)]
        if kind == "k":
            pending = self._pending.pop(u.user_id, None)
            if pending is None or pending.kind != "stop" or pending.expires_at <= self.clock.now():
                return [Reply(u.chat_id, "No pending emergency stop. Send /stop <reason> again.")]
            act = await self.trading.emergency_stop(
                actor=p.actor, reason=f"telegram: {pending.ref}", principal=p
            )
            return [
                Reply(
                    u.chat_id,
                    f"EMERGENCY STOP active. {act.approvals_revoked} approval(s) revoked, "
                    f"{len(act.entry_orders_to_cancel)} unfilled entry order(s) cancelling. "
                    "Positions were not closed. Recovery requires the portal checklist.",
                )
            ]
        return [Reply(u.chat_id, "Unknown action.")]

    async def _proposal_message(self, chat_id: int, intent_id: uuid.UUID) -> Reply:
        async with self.db.connect() as conn:
            intent = await get_intent(conn, intent_id)
            packet = await latest_packet(conn, intent_id)
        i = str(intent_id)
        return Reply(
            chat_id,
            fmt.proposal_summary(intent.trade_code, packet.packet, intent.status),
            [
                [("REVIEW DETAILS", f"v:{i}:details"), ("EVIDENCE", f"v:{i}:evidence")],
                [("COUNTERARGUMENTS", f"v:{i}:counter"), ("RISK", f"v:{i}:risk")],
                [("PORTFOLIO IMPACT", f"v:{i}:impact")],
                [("APPROVE EXACT TRADE", f"a:{i}")],
                [("REJECT", f"r:{i}"), ("REQUEST CHANGES", f"rc:{i}")],
            ],
        )

    async def _start_decision(
        self, u: Update, p: Principal, intent_id: uuid.UUID, kind: str
    ) -> Reply:
        now = self.clock.now()
        async with self.db.transaction() as conn:
            ch = await approvals.issue_challenge(
                conn, principal=p, intent_id=intent_id, channel="TELEGRAM", now=now
            )
        if kind == "a":
            self._pending[u.user_id] = _Pending(
                "approve",
                str(ch.challenge_id),
                ch.packet_hash,
                min(ch.expires_at, now + PENDING_TTL),
            )
            return Reply(
                u.chat_id,
                fmt.confirmation(ch.terms),
                [[("CONFIRM APPROVAL", f"c:{ch.challenge_id}")]],
            )
        decision: approvals.Decision = "REJECT" if kind == "r" else "REQUEST_CHANGES"
        async with self.db.transaction() as conn:
            await approvals.decide(
                conn,
                principal=p,
                token="",  # empty challenge token (ID-bound)  # nosec B106
                packet_hash=ch.packet_hash,
                decision=decision,
                now=now,
                settings=self.settings,
                channel="TELEGRAM",
                challenge_id=ch.challenge_id,
            )
        return Reply(
            u.chat_id, f"{ch.trade_code}: {'REJECTED' if kind == 'r' else 'CHANGES REQUESTED'}."
        )

    async def _finish_approval(
        self, u: Update, p: Principal, pending: _Pending, code: str | None
    ) -> Reply:
        now = self.clock.now()
        if pending.expires_at <= now:
            self._pending.pop(u.user_id, None)
            return Reply(u.chat_id, "The confirmation expired. Review the trade again.")
        second_factor = False
        if code is not None:
            async with self.db.transaction() as conn:
                second_factor = await verify_totp_for_user(
                    conn, user_id=p.user_id, code=code, settings=self.settings, now=now
                )
            if not second_factor:
                return Reply(
                    u.chat_id, "That code was wrong or already used. Nothing was approved."
                )
        self._pending.pop(u.user_id, None)
        async with self.db.transaction() as conn:
            rec = await approvals.decide(
                conn,
                principal=p,
                token="",  # empty challenge token (ID-bound)  # nosec B106
                packet_hash=pending.packet_hash,
                decision="APPROVE",
                now=now,
                settings=self.settings,
                channel="TELEGRAM",
                second_factor_ok=second_factor,
                challenge_id=uuid.UUID(pending.ref),
            )
        return Reply(
            u.chat_id,
            f"APPROVED (single use, expires {rec.expires_at:%H:%M UTC}). Risk is re-checked "
            "before submission; you will be told the venue's actual response.",
        )


def actor_for(principal: Principal) -> Actor:
    return principal.actor
