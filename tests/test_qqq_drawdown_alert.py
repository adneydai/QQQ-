import datetime as dt
import json
import unittest
from unittest.mock import patch

from src.qqq_drawdown_alert import (
    MarketDataError,
    build_pushplus_message,
    build_pushplus_payload,
    calculate_report,
    parse_alpha_vantage_daily,
    send_pushplus_message,
    should_skip_for_stale_data,
    triggered_level,
)


def make_bars(start_date, closes):
    start = dt.date.fromisoformat(start_date)
    return [
        {"date": start + dt.timedelta(days=index), "close": close}
        for index, close in enumerate(closes)
    ]


class DrawdownCalculationTests(unittest.TestCase):
    def test_calculates_60_day_and_year_drawdowns(self):
        bars = make_bars("2026-01-01", [610.0] + [500.0] * 39 + [535.0] + [470.0])

        report = calculate_report(bars)

        self.assertEqual(report.latest_date, dt.date(2026, 2, 11))
        self.assertEqual(report.latest_close, 470.0)
        self.assertEqual(report.lookback_high, 610.0)
        self.assertAlmostEqual(report.lookback_drawdown_pct, 22.9508, places=4)
        self.assertEqual(report.year_high, 610.0)
        self.assertAlmostEqual(report.year_drawdown_pct, 22.9508, places=4)

    def test_60_day_window_ignores_older_highs_but_year_high_keeps_them(self):
        bars = make_bars("2026-01-01", [610.0] + [500.0] * 69 + [535.0] + [470.0])

        report = calculate_report(bars)

        self.assertEqual(report.lookback_high, 535.0)
        self.assertAlmostEqual(report.lookback_drawdown_pct, 12.1495, places=4)
        self.assertEqual(report.year_high, 610.0)
        self.assertAlmostEqual(report.year_drawdown_pct, 22.9508, places=4)

    def test_year_high_uses_only_latest_trade_year(self):
        bars = [
            {"date": dt.date(2025, 12, 31), "close": 800.0},
            {"date": dt.date(2026, 1, 2), "close": 610.0},
            {"date": dt.date(2026, 1, 3), "close": 470.0},
        ]

        report = calculate_report(bars)

        self.assertEqual(report.year_high, 610.0)
        self.assertAlmostEqual(report.year_drawdown_pct, 22.9508, places=4)


class AlertLevelTests(unittest.TestCase):
    def test_returns_highest_triggered_level(self):
        self.assertIsNone(triggered_level(9.9))
        self.assertEqual(triggered_level(10.0), 10)
        self.assertEqual(triggered_level(19.99), 10)
        self.assertEqual(triggered_level(20.0), 20)
        self.assertEqual(triggered_level(30.0), 30)
        self.assertEqual(triggered_level(48.5), 30)


class MessageTests(unittest.TestCase):
    def test_builds_pushplus_message_with_both_alert_dimensions(self):
        bars = make_bars("2026-01-01", [610.0] + [500.0] * 69 + [535.0] + [470.0])
        report = calculate_report(bars)

        title, content = build_pushplus_message(report)

        self.assertEqual(title, "QQQ 回撤提醒：60日 -12.1% / 年内 -23.0%")
        self.assertIn("## QQQ 回撤温度计", content)
        self.assertIn("数据日：2026-03-13", content)
        self.assertIn("最新收盘价：$470.00", content)
        self.assertIn("60 日最高收盘价：$535.00", content)
        self.assertIn("提示：触达 10% 买入提醒档位", content)
        self.assertIn("当年最高收盘价：$610.00", content)
        self.assertIn("提示：触达 20% 买入提醒档位", content)
        self.assertIn("仅作提醒，不自动交易", content)

    def test_message_says_not_triggered_below_10_percent(self):
        bars = make_bars("2026-01-01", [100.0, 95.0])
        report = calculate_report(bars)

        _, content = build_pushplus_message(report)

        self.assertEqual(content.count("提示：未触达买入提醒档位"), 2)

    def test_builds_pushplus_payload(self):
        payload = build_pushplus_payload(
            token="secret-token",
            title="QQQ 回撤提醒：60日 -12.1% / 年内 -23.0%",
            content="message body",
        )

        self.assertEqual(
            payload,
            {
                "token": "secret-token",
                "title": "QQQ 回撤提醒：60日 -12.1% / 年内 -23.0%",
                "content": "message body",
                "template": "markdown",
            },
        )


class AlphaVantageParsingTests(unittest.TestCase):
    def test_parses_daily_time_series_sorted_oldest_first(self):
        payload = {
            "Time Series (Daily)": {
                "2026-01-03": {"4. close": "470.00"},
                "2026-01-02": {"4. close": "610.00"},
            }
        }

        bars = parse_alpha_vantage_daily(payload)

        self.assertEqual(
            bars,
            [
                {"date": dt.date(2026, 1, 2), "close": 610.0},
                {"date": dt.date(2026, 1, 3), "close": 470.0},
            ],
        )

    def test_raises_clear_error_for_alpha_vantage_error_payload(self):
        with self.assertRaisesRegex(MarketDataError, "Invalid API call"):
            parse_alpha_vantage_daily({"Error Message": "Invalid API call."})

    def test_raises_clear_error_when_time_series_is_missing(self):
        with self.assertRaisesRegex(MarketDataError, "missing daily time series"):
            parse_alpha_vantage_daily({"Meta Data": {}})


class FreshnessTests(unittest.TestCase):
    def test_skips_when_latest_market_date_is_not_current_new_york_date(self):
        now_utc = dt.datetime(2026, 5, 28, 23, 10, tzinfo=dt.timezone.utc)

        self.assertTrue(should_skip_for_stale_data(dt.date(2026, 5, 27), now_utc))
        self.assertFalse(should_skip_for_stale_data(dt.date(2026, 5, 28), now_utc))


class PushPlusTests(unittest.TestCase):
    def test_send_pushplus_message_posts_json_payload_and_rejects_failure_code(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps({"code": 500, "msg": "bad token"}).encode("utf-8")

        with patch("src.qqq_drawdown_alert.request.urlopen", return_value=FakeResponse()) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "bad token"):
                send_pushplus_message(
                    {"token": "secret-token", "title": "title", "content": "body", "template": "markdown"}
                )

        request_obj = urlopen.call_args.args[0]
        self.assertEqual(request_obj.full_url, "https://www.pushplus.plus/send")
        self.assertEqual(request_obj.get_header("Content-type"), "application/json")
        self.assertEqual(
            json.loads(request_obj.data.decode("utf-8")),
            {"token": "secret-token", "title": "title", "content": "body", "template": "markdown"},
        )


if __name__ == "__main__":
    unittest.main()
