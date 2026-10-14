import sys
import unittest
from datetime import date
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from session_health import validate_session


def frame(day):
    return pd.DataFrame({"close": [10]}, index=pd.to_datetime([day]))


class SessionHealthTests(unittest.TestCase):
    def test_first_stale_ticker_does_not_set_session(self):
        rows = [("STALE", frame("2026-10-12"))]
        rows += [(str(i), frame("2026-10-14")) for i in range(99)]
        session, current, coverage = validate_session(rows, date(2026, 10, 14))
        self.assertEqual(str(session.date()), "2026-10-14")
        self.assertEqual(len(current), 99)
        self.assertEqual(coverage, 99)

    def test_mixed_prices_fail_instead_of_publishing(self):
        with self.assertRaises(RuntimeError):
            validate_session([("A", frame("2026-10-12")), ("B", frame("2026-10-14"))],
                             date(2026, 10, 14))

    def test_empty_future_and_stale_fail(self):
        for rows in ([], [("A", frame("2026-10-15"))], [("A", frame("2026-10-08"))]):
            with self.assertRaises(RuntimeError):
                validate_session(rows, date(2026, 10, 14))

    def test_holiday_keeps_completed_bar(self):
        session, _, _ = validate_session([("A", frame("2026-10-13"))], date(2026, 10, 14))
        self.assertEqual(str(session.date()), "2026-10-13")
