from __future__ import annotations

import json
import sys

from assistant_runtime.settings import get_settings, load_dotenv
from tcg_tracker.image_lookup import TcgImagePriceService
from tests.image_lookup_case_fixtures import iter_image_lookup_live_cases
from tests.test_image_lookup_live_regression import _assert_outcome_matches_expected


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    load_dotenv()
    settings = get_settings()
    service = TcgImagePriceService(db_path=settings.monitor_db_path, settings=settings)

    failures: list[str] = []
    for case in iter_image_lookup_live_cases():
        outcome = service.lookup_image(case.image_path, persist=False)
        try:
            _assert_outcome_matches_expected(outcome, case.payload["expected"])
        except AssertionError as exc:
            failures.append(f"{case.case_id}: {exc}")
            continue

        summary = {
            "case_id": case.case_id,
            "status": outcome.status,
            "title": outcome.parsed.title,
            "card_number": outcome.parsed.card_number,
            "rarity": outcome.parsed.rarity,
            "set_code": outcome.parsed.set_code,
            "offers": 0 if outcome.lookup_result is None else len(outcome.lookup_result.offers),
        }
        print(json.dumps(summary, ensure_ascii=False))

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
