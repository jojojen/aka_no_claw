from __future__ import annotations

import pytest

from openclaw_adapter.artifact_output import render_text_artifact, semantic_content_instruction


def test_markdown_output_preserves_the_complete_answer() -> None:
    answer = "# Report\n\n| Name | Value |\n|---|---:|\n| A | 2 |\n"

    rendered = render_text_artifact("markdown", answer)

    assert rendered.filename == "result.md"
    assert rendered.content_type == "text/markdown"
    assert rendered.content == answer


def test_html_output_preserves_structure_links_and_text() -> None:
    answer = (
        "# Transit report\n\n| Route | Riders |\n|---|---:|\n| A | 20 |\n\n[Official source](https://example.com/data)"
    )

    rendered = render_text_artifact("html", answer)

    assert rendered.filename == "result.html"
    assert rendered.content_type == "text/html"
    assert rendered.content.startswith("<!doctype html>")
    assert "<h1>Transit report</h1>" in rendered.content
    assert "<table>" in rendered.content
    assert "<td>20</td>" in rendered.content
    assert 'href="https://example.com/data"' in rendered.content


def test_html_output_removes_active_content_without_losing_safe_content() -> None:
    rendered = render_text_artifact(
        "html",
        "# Safe\n\n<script>alert('x')</script><strong>kept</strong>",
    )

    assert "<script" not in rendered.content
    assert "alert('x')" not in rendered.content
    assert "<strong>kept</strong>" in rendered.content


def test_unknown_output_format_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported output artifact format"):
        render_text_artifact("pdf", "answer")


def test_semantic_content_instruction_keeps_file_rendering_out_of_the_answer() -> None:
    instruction = semantic_content_instruction("html")

    assert "Markdown" in instruction
    assert "不要輸出完整檔案原始碼" in instruction
    assert "不要把答案包在程式碼區塊" in instruction
    assert "html 檔案" in instruction


def test_semantic_content_instruction_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="unsupported output artifact format"):
        semantic_content_instruction("pdf")
