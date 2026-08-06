"""Render chat answers into validated text artifact formats."""

from __future__ import annotations

from dataclasses import dataclass

import markdown
import nh3


@dataclass(frozen=True, slots=True)
class RenderedTextArtifact:
    """One complete UTF-8 text file produced from a chat answer."""

    filename: str
    content_type: str
    content: str


_TEXT_ARTIFACT_SPECS = {
    "markdown": ("result.md", "text/markdown"),
    "html": ("result.html", "text/html"),
}


def output_artifact_content_type(output_format: str) -> str:
    """Return the MIME type for one validated text artifact format."""
    try:
        return _TEXT_ARTIFACT_SPECS[output_format][1]
    except KeyError as exc:
        raise ValueError(f"unsupported output artifact format: {output_format}") from exc


def semantic_content_instruction(output_format: str) -> str:
    """Tell an answer model to produce content, not a second file wrapper."""
    output_artifact_content_type(output_format)
    return (
        "請只輸出完整的語義內容，並用 Markdown 表達標題、表格、清單與連結。"
        "不要輸出完整檔案原始碼，不要把答案包在程式碼區塊，也不要要求使用者複製或另存。"
        f"輸出層會把這份內容渲染為 {output_format} 檔案並提供下載。"
    )


_HTML_STYLE = """
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { line-height: 1.6; margin: 0 auto; max-width: 72rem; padding: 2rem; }
table { border-collapse: collapse; display: block; overflow-x: auto; width: max-content; max-width: 100%; }
th, td { border: 1px solid #8888; padding: .4rem .65rem; text-align: left; }
th { background: #8882; }
code, pre { font-family: ui-monospace, monospace; }
pre { background: #8882; overflow-x: auto; padding: 1rem; }
a { color: #087ea4; overflow-wrap: anywhere; }
""".strip()


def render_text_artifact(output_format: str, answer: str) -> RenderedTextArtifact:
    """Render the complete answer through one closed output-format contract."""
    try:
        filename, content_type = _TEXT_ARTIFACT_SPECS[output_format]
    except KeyError as exc:
        raise ValueError(f"unsupported output artifact format: {output_format}") from exc
    if output_format == "markdown":
        return RenderedTextArtifact(filename, content_type, answer)
    if output_format == "html":
        fragment = markdown.markdown(answer, extensions=["tables", "fenced_code", "sane_lists"])
        safe_fragment = nh3.clean(
            fragment,
            clean_content_tags={"script", "style", "iframe", "object", "embed"},
        )
        document = (
            "<!doctype html>\n"
            '<html lang="zh-Hant">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<style>{_HTML_STYLE}</style>\n</head>\n<body>\n"
            f"{safe_fragment}\n</body>\n</html>\n"
        )
        return RenderedTextArtifact(filename, content_type, document)
    raise AssertionError("validated text artifact format has no renderer")
