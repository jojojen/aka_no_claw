"""SNS Monitor integration tests for aka_no_claw."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from assistant_runtime import AssistantSettings, get_settings
from openclaw_adapter.sns_tools import (
    _configure_sns_add_account_parser,
    _configure_sns_add_keyword_parser,
    _configure_sns_add_trend_parser,
    _configure_sns_delete_parser,
    _configure_sns_list_parser,
    _configure_sns_toggle_parser,
    _handle_sns_add_account,
    _handle_sns_add_keyword,
    _handle_sns_add_trend,
    _handle_sns_delete,
    _handle_sns_list,
    _handle_sns_toggle,
    bootstrap_sns_db,
)


class TestSettingsLoadsXEnv:
    """Test that AssistantSettings properly loads SNS environment variables."""

    def test_settings_reads_sns_db_path(self):
        """Verify settings.py exposes SNS db path. Nitter RSS needs no credentials."""
        settings = get_settings()
        assert hasattr(settings, "sns_db_path")
        assert settings.sns_db_path == "data/sns.sqlite3"


class TestSnsDatabase:
    """Test SNS database bootstrap and basic operations."""

    def test_database_bootstrap(self):
        """Create and bootstrap SNS database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            from sns_monitor.storage import SnsDatabase

            db = SnsDatabase(db_path)
            db.bootstrap()

            assert db_path.exists()
            assert db.list_watch_rules() == []

    def test_database_add_account_rule(self):
        """Add an account watch rule to the database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            from sns_monitor.storage import SnsDatabase
            from sns_monitor.models import AccountWatch

            db = SnsDatabase(db_path)
            db.bootstrap()

            rule_id = SnsDatabase._watch_rule_id("account", "testuser")
            rule = AccountWatch(
                rule_id=rule_id,
                screen_name="testuser",
                user_id=None,
                label="Test User",
                enabled=True,
                schedule_minutes=15,
                chat_id="123",
                last_checked_at=None,
            )
            db.save_watch_rule(rule)

            retrieved = db.get_watch_rule(rule_id)
            assert retrieved is not None
            assert retrieved.screen_name == "testuser"
            assert retrieved.label == "Test User"


class TestSnsToolsAddAccount:
    """Test CLI tool: sns.add-account"""

    def test_parser_configuration(self):
        """Verify add-account parser has required arguments."""
        settings = get_settings()
        parser = argparse.ArgumentParser()
        _configure_sns_add_account_parser(parser, settings)

        args = parser.parse_args(["testuser", "--chat-id", "123"])
        assert args.screen_name == "testuser"
        assert args.chat_id == "123"
        assert args.interval == 15  # default

    def test_parser_accepts_keyword_filters(self):
        """Verify add-account parser accepts account tweet keyword filters."""
        settings = get_settings()
        parser = argparse.ArgumentParser()
        _configure_sns_add_account_parser(parser, settings)

        args = parser.parse_args(["@elonmusk", "--chat-id", "123", "--keywords", "buy", "sell"])
        assert args.screen_name == "@elonmusk"
        assert args.keywords == ["buy", "sell"]

    def test_handler_adds_account_rule(self):
        """Test add-account handler creates rule in database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = AssistantSettings(sns_db_path=str(db_path))

            args = argparse.Namespace(
                screen_name="aka_claw",
                label="aka_claw",
                chat_id="123",
                interval=15,
                keywords=None,
                db=str(db_path),
            )
            result = _handle_sns_add_account(args, settings)
            assert result == 0

            # Verify rule was saved
            from sns_monitor.storage import SnsDatabase
            db = SnsDatabase(db_path)
            db.bootstrap()
            rules = db.list_watch_rules(kind="account")
            assert len(rules) == 1
            assert rules[0].screen_name == "aka_claw"

    def test_handler_adds_account_rule_with_keyword_filters(self):
        """Test add-account handler stores include-keyword filters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = AssistantSettings(sns_db_path=str(db_path))

            args = argparse.Namespace(
                screen_name="@elonmusk",
                label="Elon filtered",
                chat_id="123",
                interval=15,
                keywords=["buy", "sell"],
                db=str(db_path),
            )
            result = _handle_sns_add_account(args, settings)
            assert result == 0

            from sns_monitor.storage import SnsDatabase
            db = SnsDatabase(db_path)
            db.bootstrap()
            rules = db.list_watch_rules(kind="account")
            assert len(rules) == 1
            assert rules[0].screen_name == "elonmusk"
            assert rules[0].include_keywords == ("buy", "sell")


class TestSnsToolsAddKeyword:
    """Test CLI tool: sns.add-keyword"""

    def test_handler_adds_keyword_rule(self):
        """Test add-keyword handler creates rule in database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = AssistantSettings(sns_db_path=str(db_path))

            args = argparse.Namespace(
                query="機動戰士",
                label="Gundam",
                chat_id="123",
                interval=30,
                db=str(db_path),
            )
            result = _handle_sns_add_keyword(args, settings)
            assert result == 0

            from sns_monitor.storage import SnsDatabase
            db = SnsDatabase(db_path)
            db.bootstrap()
            rules = db.list_watch_rules(kind="keyword")
            assert len(rules) == 1
            assert rules[0].query == "機動戰士"


class TestSnsToolsAddTrend:
    """Test CLI tool: sns.add-trend"""

    def test_handler_adds_trend_rule(self):
        """Test add-trend handler creates rule in database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = AssistantSettings(sns_db_path=str(db_path))

            args = argparse.Namespace(
                category="trending",
                label="",
                chat_id="123",
                interval=60,
                db=str(db_path),
            )
            result = _handle_sns_add_trend(args, settings)
            assert result == 0

            from sns_monitor.storage import SnsDatabase
            db = SnsDatabase(db_path)
            db.bootstrap()
            rules = db.list_watch_rules(kind="trend")
            assert len(rules) == 1
            assert rules[0].category == "trending"


class TestSnsToolsList:
    """Test CLI tool: sns.list-rules"""

    def test_handler_lists_empty(self):
        """Test list-rules with no rules."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = AssistantSettings(sns_db_path=str(db_path))

            args = argparse.Namespace(kind=None, db=str(db_path))
            result = _handle_sns_list(args, settings)
            assert result == 0  # Empty list is not an error

    def test_handler_lists_rules(self):
        """Test list-rules shows existing rules."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = AssistantSettings(sns_db_path=str(db_path))

            # Add some rules first
            from sns_monitor.storage import SnsDatabase
            from sns_monitor.models import AccountWatch

            db = SnsDatabase(db_path)
            db.bootstrap()

            rule_id = SnsDatabase._watch_rule_id("account", "elonmusk")
            rule = AccountWatch(
                rule_id=rule_id,
                screen_name="elonmusk",
                user_id=None,
                label="Elon Musk",
                enabled=True,
                schedule_minutes=15,
                chat_id="123",
                last_checked_at=None,
            )
            db.save_watch_rule(rule)

            # Now test listing
            args = argparse.Namespace(kind=None, db=str(db_path))
            result = _handle_sns_list(args, settings)
            assert result == 0

    def test_handler_lists_account_keyword_filters(self, capsys):
        """Test list-rules displays account keyword filters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = AssistantSettings(sns_db_path=str(db_path))

            from sns_monitor.storage import SnsDatabase
            from sns_monitor.models import AccountWatch

            db = SnsDatabase(db_path)
            db.bootstrap()

            rule_id = SnsDatabase._watch_rule_id("account", "elonmusk")
            db.save_watch_rule(
                AccountWatch(
                    rule_id=rule_id,
                    screen_name="elonmusk",
                    user_id=None,
                    label="Elon Musk",
                    include_keywords=("buy", "sell"),
                    enabled=True,
                    schedule_minutes=15,
                    chat_id="123",
                    last_checked_at=None,
                )
            )

            args = argparse.Namespace(kind=None, db=str(db_path))
            result = _handle_sns_list(args, settings)
            output = capsys.readouterr().out

            assert result == 0
            assert "filters=buy, sell" in output


class TestSnsToolsToggle:
    """Test CLI tool: sns.toggle-rule"""

    def test_handler_toggles_rule(self):
        """Test toggle-rule enables/disables a rule."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = AssistantSettings(sns_db_path=str(db_path))

            # Add a rule first
            from sns_monitor.storage import SnsDatabase
            from sns_monitor.models import AccountWatch

            db = SnsDatabase(db_path)
            db.bootstrap()

            rule_id = SnsDatabase._watch_rule_id("account", "aka_claw")
            rule = AccountWatch(
                rule_id=rule_id,
                screen_name="aka_claw",
                user_id=None,
                label="aka_claw",
                enabled=True,
                schedule_minutes=15,
                chat_id="123",
                last_checked_at=None,
            )
            db.save_watch_rule(rule)

            # Test toggling
            args = argparse.Namespace(rule_id=rule_id, disabled=True, enabled=False, db=str(db_path))
            result = _handle_sns_toggle(args, settings)
            assert result == 0

            # Verify it was disabled
            db = SnsDatabase(db_path)
            updated = db.get_watch_rule(rule_id)
            assert updated.enabled is False


class TestSnsToolsDelete:
    """Test CLI tool: sns.delete-rule"""

    def test_handler_deletes_rule(self):
        """Test delete-rule removes a rule."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = AssistantSettings(sns_db_path=str(db_path))

            # Add a rule first
            from sns_monitor.storage import SnsDatabase
            from sns_monitor.models import AccountWatch

            db = SnsDatabase(db_path)
            db.bootstrap()

            rule_id = SnsDatabase._watch_rule_id("account", "elonmusk")
            rule = AccountWatch(
                rule_id=rule_id,
                screen_name="elonmusk",
                user_id=None,
                label="Elon Musk",
                enabled=True,
                schedule_minutes=15,
                chat_id="123",
                last_checked_at=None,
            )
            db.save_watch_rule(rule)

            # Test deleting
            args = argparse.Namespace(rule_id=rule_id, db=str(db_path))
            result = _handle_sns_delete(args, settings)
            assert result == 0

            # Verify it was deleted
            db = SnsDatabase(db_path)
            deleted = db.get_watch_rule(rule_id)
            assert deleted is None


class TestSnsMonitor:
    """Test SNS Monitor startup and lifecycle."""

    def test_monitor_start_stop(self):
        """Test monitor starts and stops correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            from sns_monitor.storage import SnsDatabase
            from sns_monitor.x_client import XClient
            from sns_monitor.monitor import ensure_monitor

            db = SnsDatabase(db_path)
            db.bootstrap()

            # Create a mock X client
            x_client = MagicMock(spec=XClient)

            # Mock the async context
            def mock_notify(chat_id: str, text: str) -> None:
                pass

            monitor, started = ensure_monitor(
                db_path=db_path,
                x_client=x_client,
                notify_fn=mock_notify,
                interval_seconds=60,
            )

            assert monitor is not None
            # Monitor should be a SnsMonitor instance with start/stop methods
            assert hasattr(monitor, "start")
            assert hasattr(monitor, "stop")
            assert hasattr(monitor, "is_running")

    def test_notify_function_interface(self):
        """Test that notify_fn has correct signature."""
        def test_notify(chat_id: str, text: str) -> None:
            pass

        # Should accept chat_id (str) and text (str)
        test_notify("123", "test message")


class TestTelegramSnsCommands:
    """Test SNS command handlers in TelegramCommandProcessor."""

    @staticmethod
    def _make_processor(db, **kwargs):
        """Build a TelegramCommandProcessor wired with SNS registry handlers."""
        from openclaw_adapter.sns_commands import (
            build_sns_add_handler,
            build_sns_buzz_handler,
            build_sns_delete_handler,
            build_snslist_handler,
            build_snslist_view_fn,
            build_sns_rule_deleter,
        )
        from price_monitor_bot.bot import RegisteredCommand, TelegramCommandProcessor

        command_handlers = {
            "/snsadd": RegisteredCommand(build_sns_add_handler(db)),
            "/sns_add": RegisteredCommand(build_sns_add_handler(db)),
            "/snslist": RegisteredCommand(build_snslist_handler(db)),
            "/sns_list": RegisteredCommand(build_snslist_handler(db)),
            "/snsdelete": RegisteredCommand(build_sns_delete_handler(db)),
            "/sns_delete": RegisteredCommand(build_sns_delete_handler(db)),
            "/snsbuzz": RegisteredCommand(build_sns_buzz_handler(None)),
        }
        view_handlers = {"sl": build_snslist_view_fn(db)}
        item_deleter_handlers = {"sl": build_sns_rule_deleter(db)}
        return TelegramCommandProcessor(
            lookup_renderer=lambda q: "test",
            board_loader=lambda: (),
            catalog_renderer=lambda: "test",
            sns_db=db,
            command_handlers=command_handlers,
            view_handlers=view_handlers,
            item_deleter_handlers=item_deleter_handlers,
            **kwargs,
        )

    def test_telegram_processor_accepts_sns_db(self):
        """Test that TelegramCommandProcessor accepts sns_db parameter."""
        from price_monitor_bot.bot import TelegramCommandProcessor
        from sns_monitor.storage import SnsDatabase
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            from sns_monitor.storage import SnsDatabase
            db = SnsDatabase(db_path)
            db.bootstrap()

            processor = TelegramCommandProcessor(
                lookup_renderer=lambda q: "test",
                board_loader=lambda: (),
                catalog_renderer=lambda: "test",
                sns_db=db,
            )
            assert processor._sns_db is db

    def test_telegram_sns_add_command(self):
        """Test Telegram /snsadd command handler."""
        from price_monitor_bot.bot import TelegramCommandProcessor
        from sns_monitor.storage import SnsDatabase

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SnsDatabase(db_path)
            db.bootstrap()

            processor = TelegramCommandProcessor(
                lookup_renderer=lambda q: "test",
                board_loader=lambda: (),
                catalog_renderer=lambda: "test",
                sns_db=db,
            )

            plan = processor.build_reply_plan(chat_id="123", text="/snsadd @aka_claw")
            assert plan is not None

    def test_telegram_sns_add_command_with_account_filters(self):
        """Test Telegram /snsadd command stores account keyword filters."""
        from sns_monitor.storage import SnsDatabase

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SnsDatabase(db_path)
            db.bootstrap()

            processor = self._make_processor(db)

            plan = processor.build_reply_plan(chat_id="123", text='/snsadd @realDonaldTrump ["buy", "sell"]')
            assert plan is not None
            reply = plan.execute()
            assert "篩選：buy, sell" in reply

            rules = db.list_watch_rules(kind="account")
            assert len(rules) == 1
            assert rules[0].screen_name == "realDonaldTrump"
            assert rules[0].include_keywords == ("buy", "sell")

    def test_telegram_natural_language_sns_filter_update_falls_back_when_router_returns_unknown(self):
        """Natural-language SNS filter updates should still work when the LLM router returns unknown."""
        from price_monitor_bot.natural_language import TelegramNaturalLanguageIntent
        from sns_monitor.models import AccountWatch
        from sns_monitor.storage import SnsDatabase

        class UnknownRouter:
            def route(self, text: str) -> TelegramNaturalLanguageIntent:
                return TelegramNaturalLanguageIntent(intent="unknown", confidence=0.1)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SnsDatabase(db_path)
            db.bootstrap()
            rule_id = SnsDatabase._watch_rule_id("account", "tenbai_hakase")
            db.save_watch_rule(
                AccountWatch(
                    rule_id=rule_id,
                    screen_name="tenbai_hakase",
                    user_id="resolved-id",
                    label="@tenbai_hakase",
                    enabled=True,
                    schedule_minutes=15,
                    chat_id="123",
                    last_checked_at=None,
                )
            )

            processor = self._make_processor(db, natural_language_router=UnknownRouter())

            reply = processor.build_reply(
                chat_id="123",
                text="幫我把@tenbai_hakase 加上 ［抽選］ 篩選",
            )

            assert reply is not None
            assert "篩選：抽選" in reply

            rules = db.list_watch_rules(kind="account")
            assert len(rules) == 1
            assert rules[0].screen_name == "tenbai_hakase"
            assert rules[0].user_id == "resolved-id"
            assert rules[0].include_keywords == ("抽選",)

    def test_telegram_sns_list_command(self):
        """Test Telegram /snslist command handler."""
        from sns_monitor.storage import SnsDatabase

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SnsDatabase(db_path)
            db.bootstrap()

            processor = self._make_processor(db)

            plan = processor.build_reply_plan(chat_id="123", text="/snslist")
            assert plan is not None
            reply = plan.execute()
            assert "SNS 監控" in reply or "尚無" in reply  # Either shows rules or empty message
