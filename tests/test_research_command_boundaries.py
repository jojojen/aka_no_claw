"""R3.0 compatibility pins for the research-pipeline extraction."""

from __future__ import annotations

import inspect
import time

from openclaw_adapter import research_command
from openclaw_adapter.research.models import ResearchBudget, ResearchJobContext, ResearchSectionResult


def test_research_public_surface_is_available() -> None:
    for name in (
        "ResearchCommandService",
        "ResearchBudget",
        "ResearchTarget",
        "ResearchReport",
        "parse_research_target",
        "build_research_handler",
        "format_research_full_report",
    ):
        assert hasattr(research_command, name), name


def test_budgeted_search_and_normalization_remain_free_functions() -> None:
    assert inspect.isfunction(research_command.build_budgeted_search_fn)
    assert inspect.isfunction(research_command.parse_research_target)


def test_stage_result_envelope_retains_provenance_and_terminal_metadata() -> None:
    notifier = type("Notifier", (), {"send": lambda self, _text: None})()
    ctx = ResearchJobContext(
        raw_input="fixture", chat_id="76", notifier=notifier,
        budget=ResearchBudget(max_searches=1), search_fn=lambda *_args: (),
        stage_started_monotonic=time.monotonic(),
    )
    ctx.add_section_result(ResearchSectionResult(
        section_name="fixture", status="partial", confidence=0.2,
        sample_count=0, evidence_count=0, summary="source unavailable",
        evidence_urls=("https://example.invalid/evidence",),
    ))
    result = ctx.section_results[0]
    assert result.schema_version == 1
    assert result.payload == (("summary", "source unavailable"),)
    assert result.provenance_urls == result.evidence_urls
    assert result.failure_class == "partial"
    assert result.elapsed_seconds is not None
