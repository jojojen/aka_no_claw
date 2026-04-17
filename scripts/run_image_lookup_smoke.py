from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from assistant_runtime.settings import get_settings, load_dotenv
from tcg_tracker.image_lookup import TcgImagePriceService


@dataclass(frozen=True, slots=True)
class SmokeExpectation:
    relative_path: str
    expected_status: str
    expected_game: str | None = None
    expected_card_number: str | None = None


EXPECTATIONS = (
    SmokeExpectation("fwdspecptcg/charizard.jpg", expected_status="success", expected_game="pokemon", expected_card_number="223/193"),
    SmokeExpectation("fwdspecptcg/ririe.jpg", expected_status="success", expected_game="pokemon", expected_card_number="126/100"),
    SmokeExpectation(
        ".openclaw_tmp/telegram-upload-charizard_sv2a_official.jpg",
        expected_status="success",
        expected_game="pokemon",
        expected_card_number="201/165",
    ),
)

SAMPLE_PATHS = (
    "fwdspecptcg/charizard.jpg",
    "fwdspecptcg/PICKACHU2.jpg",
    "fwdspecptcg/PICKACHU3.jpg",
    "fwdspecptcg/pikachu.jpg",
    "fwdspecptcg/ririe.jpg",
    ".openclaw_tmp/telegram-upload-charizard_sv2a_official.jpg",
)


def main() -> int:
    _configure_stdout()
    load_dotenv()
    settings = get_settings()
    service = TcgImagePriceService(db_path=settings.monitor_db_path, settings=settings)

    failures: list[str] = []
    print("Image lookup smoke test")
    print("=======================")

    for relative_path in SAMPLE_PATHS:
        path = ROOT / relative_path
        if not path.exists():
            print(f"- {relative_path}: skipped (missing file)")
            continue

        outcome = service.lookup_image(path, persist=False)
        fair_value = None
        offer_count = 0
        if outcome.lookup_result is not None:
            offer_count = len(outcome.lookup_result.offers)
            if outcome.lookup_result.fair_value is not None:
                fair_value = outcome.lookup_result.fair_value.amount_jpy

        print(
            f"- {relative_path}: status={outcome.status} game={outcome.parsed.game} "
            f"title={outcome.parsed.title!r} card_number={outcome.parsed.card_number!r} "
            f"rarity={outcome.parsed.rarity!r} set_code={outcome.parsed.set_code!r} "
            f"offers={offer_count} fair={fair_value}"
        )
        if outcome.warnings:
            print(f"  warnings={outcome.warnings}")

    for expectation in EXPECTATIONS:
        path = ROOT / expectation.relative_path
        if not path.exists():
            failures.append(f"{expectation.relative_path}: expected smoke sample is missing")
            continue

        outcome = service.lookup_image(path, persist=False)
        if outcome.status != expectation.expected_status:
            failures.append(
                f"{expectation.relative_path}: expected status {expectation.expected_status}, got {outcome.status}"
            )
        if expectation.expected_game is not None and outcome.parsed.game != expectation.expected_game:
            failures.append(
                f"{expectation.relative_path}: expected game {expectation.expected_game}, got {outcome.parsed.game}"
            )
        if expectation.expected_card_number is not None and outcome.parsed.card_number != expectation.expected_card_number:
            failures.append(
                f"{expectation.relative_path}: expected card number {expectation.expected_card_number}, got {outcome.parsed.card_number}"
            )

    if failures:
        print("\nSmoke failures")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("\nSmoke expectations passed.")
    return 0


def _configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        return


if __name__ == "__main__":
    raise SystemExit(main())
