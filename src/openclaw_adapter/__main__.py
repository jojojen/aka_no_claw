from __future__ import annotations

import argparse
import logging

from assistant_runtime import configure_logging, get_settings, load_dotenv

from .toolset import build_tool_registry, render_tool_catalog


def main() -> int:
    load_dotenv()
    settings = get_settings()
    configure_logging(settings)
    logging.getLogger(__name__).info(
        "OpenClaw entrypoint initialized env=%s db=%s log_file=%s",
        settings.monitor_env,
        settings.monitor_db_path,
        settings.log_file_path,
    )
    registry = build_tool_registry(settings)
    parser = argparse.ArgumentParser(description="OpenClaw personal-assistant entrypoint.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_tools_parser = subparsers.add_parser("list-tools", help="List all assistant tools currently registered.")
    list_tools_parser.set_defaults(_assistant_handler=lambda args: _handle_list_tools(registry))
    registry.install(subparsers)

    args = parser.parse_args()
    return args._assistant_handler(args)


def _handle_list_tools(registry) -> int:
    print(render_tool_catalog(registry))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
