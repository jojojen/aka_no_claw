from openclaw_adapter.dynamic_tools.knowledge_context import ContextBudget, bounded_block
from openclaw_adapter.dynamic_tools.repair import RepairBudget
from openclaw_adapter.dynamic_tools.sandbox import TerminalCleanup


def test_context_budget_and_block_are_bounded() -> None:
    budget = ContextBudget(limit=2)
    assert budget.grant(5) == 2
    assert budget.exhausted
    assert len(bounded_block(["abc", "def"], max_chars=4)) <= 6


def test_terminal_cleanup_runs_once_on_exception() -> None:
    calls: list[str] = []
    try:
        with TerminalCleanup() as cleanup:
            cleanup.add(lambda: calls.append("clean"))
            raise RuntimeError("stop")
    except RuntimeError:
        pass
    assert calls == ["clean"]


def test_repair_budget_stops_repeated_or_excessive_attempts() -> None:
    budget = RepairBudget(limit=2)
    assert budget.accept("one")
    assert not budget.accept("one")
    assert budget.accept("two")
    assert not budget.accept("three")
