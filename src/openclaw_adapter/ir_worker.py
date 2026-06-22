"""Short-lived BroadLink IR worker.

Long-running Telegram / web bridge processes can get stuck with BroadLink UDP
auth returning ``Errno 65 No route to host`` while a fresh CLI process succeeds.
This worker keeps BroadLink socket state isolated per IR action.
"""

from __future__ import annotations

import argparse
import os

from assistant_runtime import configure_logging, get_settings, load_dotenv

from . import ir_command


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one BroadLink IR action.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("discover")
    learn = sub.add_parser("learn")
    learn.add_argument("device")
    learn.add_argument("button")
    send = sub.add_parser("send")
    send.add_argument("device")
    send.add_argument("button")
    args = parser.parse_args()

    os.environ[ir_command._WORKER_ENV] = "1"  # noqa: SLF001 - intentional worker guard.
    load_dotenv()
    settings = get_settings()
    configure_logging(settings)

    if args.action == "discover":
        print(ir_command._discover_message_inline(settings))  # noqa: SLF001
        return 0
    if args.action == "learn":
        print(ir_command._learn_code_inline(settings, args.device, args.button))  # noqa: SLF001
        return 0
    if args.action == "send":
        print(ir_command._send_code_inline(settings, args.device, args.button))  # noqa: SLF001
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
