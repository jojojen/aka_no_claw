"""In-memory security signals for the OpenClaw Telegram polling process."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from threading import Lock
from typing import Protocol


class TelegramClient(Protocol):
    def get_me(self) -> dict[str, object]: ...

    def get_webhook_info(self) -> dict[str, object]: ...

    def send_message(self, *, chat_id: str | int, text: str, reply_markup=None) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class UnauthorizedChatAttempt:
    first_seen: datetime
    last_seen: datetime
    count: int


class TelegramSecurityMonitor:
    """Tracks suspicious local Telegram signals and notifies allowed operators.

    State intentionally lives only for the lifetime of the polling process. It
    avoids retaining message content or full tokens while still making active
    anomalies visible through Telegram and the ``/security`` command.
    """

    def __init__(
        self,
        *,
        alert_chat_ids: frozenset[str],
        token: str,
        unauthorized_warning_threshold: int = 3,
    ) -> None:
        self._alert_chat_ids = alert_chat_ids
        self._fingerprint = f"tg_{sha256(token.encode()).hexdigest()[:6]}"
        self._threshold = max(1, unauthorized_warning_threshold)
        self._lock = Lock()
        self._unauthorized: dict[str, UnauthorizedChatAttempt] = {}
        self._polling_conflicts = 0
        self._last_health_check: datetime | None = None
        self._bot_username: str | None = None
        self._webhook_configured: bool | None = None
        self._health_error: str | None = None
        self._active_alerts: set[str] = set()

    def handle_event(self, event_type: str, details: dict[str, object], client: TelegramClient) -> None:
        if event_type == "unauthorized_chat":
            self._record_unauthorized_chat(str(details.get("chat_id", "unknown")), client)
        elif event_type == "polling_conflict":
            self._record_polling_conflict(client)

    def run_health_check(self, client: TelegramClient) -> None:
        now = datetime.now()
        try:
            me = client.get_me()
            username = str(me.get("username") or "").strip() or None
            webhook = client.get_webhook_info()
            webhook_url = str(webhook.get("url") or "").strip()
        except Exception as exc:
            with self._lock:
                self._last_health_check = now
                self._health_error = f"{type(exc).__name__}: {exc}"
                should_alert = "health_failure" not in self._active_alerts
                self._active_alerts.add("health_failure")
            if should_alert:
                self._notify(client, "WARNING", "Telegram health check failed.", "Check network access and bot token validity.")
            return

        with self._lock:
            username_changed = self._bot_username is not None and username != self._bot_username
            self._bot_username = username
            self._webhook_configured = bool(webhook_url)
            self._last_health_check = now
            self._health_error = None
            self._active_alerts.discard("health_failure")
            webhook_alert = bool(webhook_url) and "webhook_configured" not in self._active_alerts
            if webhook_alert:
                self._active_alerts.add("webhook_configured")
            if not webhook_url:
                self._active_alerts.discard("webhook_configured")
            username_alert = username_changed and "username_changed" not in self._active_alerts
            if username_alert:
                self._active_alerts.add("username_changed")
        if webhook_alert:
            self._notify(client, "WARNING", "A Telegram webhook is configured while polling is active.", "Check webhook ownership and remove it if unexpected.")
        if username_alert:
            self._notify(client, "HIGH", "Telegram bot identity changed during polling.", "Verify the token and rotate it if this was not expected.")

    def render_status(self) -> str:
        with self._lock:
            now = datetime.now()
            recent_unauthorized = sum(
                attempt.count
                for attempt in self._unauthorized.values()
                if (now - attempt.last_seen).total_seconds() <= 24 * 60 * 60
            )
            health = "OK" if self._last_health_check is not None and self._health_error is None else "FAILED"
            last_check = self._last_health_check.strftime("%Y-%m-%d %H:%M") if self._last_health_check else "never"
            webhook = "unknown" if self._webhook_configured is None else ("yes" if self._webhook_configured else "no")
            return "\n".join((
                "Telegram security status",
                "",
                f"Bot health: {health}",
                f"Unauthorized chats (24h): {recent_unauthorized}",
                f"Polling conflicts: {self._polling_conflicts}",
                f"Webhook configured: {webhook}",
                f"Last health check: {last_check}",
                f"Token fingerprint: {self._fingerprint}",
            ))

    def _record_unauthorized_chat(self, chat_id: str, client: TelegramClient) -> None:
        now = datetime.now()
        with self._lock:
            previous = self._unauthorized.get(chat_id)
            attempt = UnauthorizedChatAttempt(
                first_seen=previous.first_seen if previous else now,
                last_seen=now,
                count=(previous.count if previous else 0) + 1,
            )
            self._unauthorized[chat_id] = attempt
            should_alert = previous is None or attempt.count == self._threshold
        if should_alert:
            severity = "INFO" if previous is None else "WARNING"
            detail = "A new unauthorized Telegram chat attempted access." if previous is None else (
                f"An unauthorized Telegram chat reached {attempt.count} rejected attempts."
            )
            self._notify(client, severity, detail, "Review the allowlist and investigate unexpected access attempts.")

    def _record_polling_conflict(self, client: TelegramClient) -> None:
        with self._lock:
            self._polling_conflicts += 1
            should_alert = "polling_conflict" not in self._active_alerts
            self._active_alerts.add("polling_conflict")
        if should_alert:
            self._notify(
                client,
                "HIGH",
                "Detected Telegram polling conflict.",
                "Check for duplicate OpenClaw instances. If unexpected, rotate the bot token via BotFather.",
            )

    def _notify(self, client: TelegramClient, severity: str, detail: str, recommendation: str) -> None:
        text = f"⚠️ Security Alert [{severity}]\n\n{detail}\n\nRecommended action: {recommendation}"
        for chat_id in self._alert_chat_ids:
            try:
                client.send_message(chat_id=chat_id, text=text)
            except Exception:
                # A security notification failure must not stop polling.
                continue
