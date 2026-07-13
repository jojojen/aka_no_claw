from __future__ import annotations

from openclaw_adapter.telegram_security import TelegramSecurityMonitor


class FakeTelegramClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.me: dict[str, object] = {"username": "aka_bot"}
        self.webhook: dict[str, object] = {"url": ""}

    def get_me(self) -> dict[str, object]:
        return self.me

    def get_webhook_info(self) -> dict[str, object]:
        return self.webhook

    def send_message(self, *, chat_id: str | int, text: str, reply_markup=None) -> dict[str, object]:
        self.messages.append((str(chat_id), text))
        return {}


def test_unauthorized_chat_is_counted_and_warned_at_threshold() -> None:
    client = FakeTelegramClient()
    monitor = TelegramSecurityMonitor(
        alert_chat_ids=frozenset({"operator"}),
        token="secret-token",
        unauthorized_warning_threshold=3,
    )

    for _ in range(3):
        monitor.handle_event("unauthorized_chat", {"chat_id": "unknown"}, client)

    status = monitor.render_status()
    assert "Unauthorized chats (24h): 3" in status
    assert len(client.messages) == 2
    assert "[INFO]" in client.messages[0][1]
    assert "[WARNING]" in client.messages[1][1]
    assert "secret-token" not in status
    assert "Token fingerprint: tg_" in status


def test_polling_conflict_alert_is_high_severity_and_deduplicated() -> None:
    client = FakeTelegramClient()
    monitor = TelegramSecurityMonitor(alert_chat_ids=frozenset({"operator"}), token="secret")

    monitor.handle_event("polling_conflict", {}, client)
    monitor.handle_event("polling_conflict", {}, client)

    assert "Polling conflicts: 2" in monitor.render_status()
    assert len(client.messages) == 1
    assert "[HIGH]" in client.messages[0][1]
    assert "rotate the bot token" in client.messages[0][1]


def test_health_check_reports_webhook_and_security_status() -> None:
    client = FakeTelegramClient()
    monitor = TelegramSecurityMonitor(alert_chat_ids=frozenset({"operator"}), token="secret")

    monitor.run_health_check(client)
    assert "Bot health: OK" in monitor.render_status()
    assert "Webhook configured: no" in monitor.render_status()

    client.webhook = {"url": "https://example.test/hook"}
    monitor.run_health_check(client)

    assert "Webhook configured: yes" in monitor.render_status()
    assert len(client.messages) == 1
    assert "webhook" in client.messages[0][1].lower()
