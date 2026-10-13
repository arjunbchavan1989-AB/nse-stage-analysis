import unittest
import pandas as pd
from run import rs_percentile


class RelativeStrengthTests(unittest.TestCase):
    def test_common_session_returns(self):
        dates = pd.bdate_range("2026-01-01", periods=64)
        stock = pd.DataFrame({"close": [100.] * 63 + [120.]}, index=dates)
        bench = pd.DataFrame({"close": [100.] * 63 + [110.]}, index=dates)
        self.assertAlmostEqual(rs_percentile(stock, bench), .10)

    def test_insufficient_overlap_has_no_score(self):
        dates = pd.bdate_range("2026-01-01", periods=64)
        stock = pd.DataFrame({"close": [100.] * 64}, index=dates)
        self.assertIsNone(rs_percentile(stock, stock.iloc[1:]))


if __name__ == "__main__":
    unittest.main()
