from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Callable

ToolHandler = Callable[[argparse.Namespace], int]
ParserConfigurator = Callable[[argparse.ArgumentParser], None]


@dataclass(frozen=True, slots=True)
class AssistantTool:
    name: str
    description: str
    configure_parser: ParserConfigurator
    handler: ToolHandler
    aliases: tuple[str, ...] = field(default_factory=tuple)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: list[AssistantTool] = []

    def register(self, tool: AssistantTool) -> None:
        self._tools.append(tool)

    def tools(self) -> tuple[AssistantTool, ...]:
        return tuple(self._tools)

    def install(self, subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        for tool in self._tools:
            parser = subparsers.add_parser(
                tool.name,
                help=tool.description,
                description=tool.description,
                aliases=list(tool.aliases),
            )
            tool.configure_parser(parser)
            parser.set_defaults(_assistant_handler=tool.handler, _assistant_tool_name=tool.name)
