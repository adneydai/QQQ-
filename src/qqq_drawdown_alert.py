import datetime as dt
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib import parse, request
from zoneinfo import ZoneInfo


SYMBOL = "QQQ"
LOOKBACK_DAYS = 60
THRESHOLDS = (10, 20, 30)
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
PUSHPLUS_URL = "https://www.pushplus.plus/send"
NEW_YORK_TZ = ZoneInfo("America/New_York")


class MarketDataError(RuntimeError):
    """Raised when market data cannot be parsed or trusted."""


@dataclass(frozen=True)
class DrawdownReport:
    symbol: str
    latest_date: dt.date
    latest_close: float
    lookback_high: float
    lookback_drawdown_pct: float
    lookback_alert_level: int | None
    year_high: float
    year_drawdown_pct: float
    year_alert_level: int | None


def parse_alpha_vantage_daily(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for error_key in ("Error Message", "Note", "Information"):
        if error_key in payload:
            raise MarketDataError(str(payload[error_key]))

    series = payload.get("Time Series (Daily)")
    if not isinstance(series, dict):
        raise MarketDataError("Alpha Vantage payload is missing daily time series")

    bars = []
    for date_text, values in series.items():
        if not isinstance(values, dict) or "4. close" not in values:
            raise MarketDataError(f"Alpha Vantage row for {date_text} is missing close price")
        try:
            bar_date = dt.date.fromisoformat(date_text)
            close = float(values["4. close"])
        except (TypeError, ValueError) as exc:
            raise MarketDataError(f"Alpha Vantage row for {date_text} is invalid") from exc
        bars.append({"date": bar_date, "close": close})

    if not bars:
        raise MarketDataError("Alpha Vantage daily time series is empty")

    return sorted(bars, key=lambda bar: bar["date"])


def calculate_report(
    bars: list[dict[str, Any]],
    symbol: str = SYMBOL,
    lookback_days: int = LOOKBACK_DAYS,
) -> DrawdownReport:
    if not bars:
        raise MarketDataError("No market bars available")

    sorted_bars = sorted(bars, key=lambda bar: bar["date"])
    latest = sorted_bars[-1]
    latest_date = latest["date"]
    latest_close = float(latest["close"])

    lookback_bars = sorted_bars[-lookback_days:]
    lookback_high = max(float(bar["close"]) for bar in lookback_bars)

    latest_year = latest_date.year
    year_bars = [bar for bar in sorted_bars if bar["date"].year == latest_year]
    year_high = max(float(bar["close"]) for bar in year_bars)

    lookback_drawdown = drawdown_pct(lookback_high, latest_close)
    year_drawdown = drawdown_pct(year_high, latest_close)

    return DrawdownReport(
        symbol=symbol,
        latest_date=latest_date,
        latest_close=latest_close,
        lookback_high=lookback_high,
        lookback_drawdown_pct=lookback_drawdown,
        lookback_alert_level=triggered_level(lookback_drawdown),
        year_high=year_high,
        year_drawdown_pct=year_drawdown,
        year_alert_level=triggered_level(year_drawdown),
    )


def drawdown_pct(high: float, current: float) -> float:
    if high <= 0:
        raise MarketDataError("High price must be positive")
    return max(0.0, (high - current) / high * 100)


def triggered_level(drawdown: float, thresholds: tuple[int, ...] = THRESHOLDS) -> int | None:
    triggered = [level for level in thresholds if drawdown >= level]
    return max(triggered) if triggered else None


def build_pushplus_message(report: DrawdownReport) -> tuple[str, str]:
    title = (
        f"{report.symbol} 回撤提醒：60日 {format_drawdown(report.lookback_drawdown_pct)} / "
        f"年内 {format_drawdown(report.year_drawdown_pct)}"
    )
    content = "\n".join(
        [
            f"## {report.symbol} 回撤温度计",
            "",
            f"数据日：{report.latest_date.isoformat()}",
            f"最新收盘价：{format_price(report.latest_close)}",
            "",
            "### 60 日波段回撤",
            f"60 日最高收盘价：{format_price(report.lookback_high)}",
            f"当前回撤：{format_drawdown(report.lookback_drawdown_pct)}",
            "",
            f"提示：{format_alert(report.lookback_alert_level)}",
            "",
            "### 当年高点回撤",
            f"当年最高收盘价：{format_price(report.year_high)}",
            f"当前回撤：{format_drawdown(report.year_drawdown_pct)}",
            "",
            f"提示：{format_alert(report.year_alert_level)}",
            "",
            "---",
            "口径：均按收盘价计算；仅作提醒，不自动交易。",
        ]
    )
    return title, content


def build_pushplus_payload(token: str, title: str, content: str) -> dict[str, str]:
    return {
        "token": token,
        "title": title,
        "content": content,
        "template": "markdown",
    }


def should_skip_for_stale_data(latest_date: dt.date, now_utc: dt.datetime | None = None) -> bool:
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)
    current_new_york_date = now_utc.astimezone(NEW_YORK_TZ).date()
    return latest_date != current_new_york_date


def fetch_alpha_vantage_daily(api_key: str, symbol: str = SYMBOL) -> dict[str, Any]:
    query = parse.urlencode(
        {
            "function": "TIME_SERIES_DAILY",
            "symbol": symbol,
            "outputsize": "full",
            "apikey": api_key,
        }
    )
    url = f"{ALPHA_VANTAGE_URL}?{query}"
    req = request.Request(url, headers={"User-Agent": "qqq-drawdown-alert/1.0"})
    with request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def send_pushplus_message(payload: dict[str, str]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        PUSHPLUS_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=15) as response:
        response_body = response.read().decode("utf-8")
        if getattr(response, "status", 200) >= 400:
            raise RuntimeError(f"PushPlus HTTP error {response.status}: {response_body}")

    result = json.loads(response_body) if response_body else {}
    if result.get("code") != 200:
        message = result.get("msg") or result.get("message") or response_body
        raise RuntimeError(f"PushPlus send failed: {message}")


def format_price(value: float) -> str:
    return f"${value:.2f}"


def format_drawdown(value: float) -> str:
    if round(value, 1) == 0:
        return "0.0%"
    return f"-{value:.1f}%"


def format_alert(level: int | None) -> str:
    if level is None:
        return "未触达买入提醒档位"
    return f"触达 {level}% 买入提醒档位"


def run() -> int:
    alpha_key = os.environ.get("ALPHA_VANTAGE_API_KEY")
    pushplus_token = os.environ.get("PUSHPLUS_TOKEN")
    if not alpha_key:
        raise RuntimeError("ALPHA_VANTAGE_API_KEY is not set")
    if not pushplus_token:
        raise RuntimeError("PUSHPLUS_TOKEN is not set")

    market_payload = fetch_alpha_vantage_daily(alpha_key)
    bars = parse_alpha_vantage_daily(market_payload)
    report = calculate_report(bars)

    if should_skip_for_stale_data(report.latest_date):
        print(f"Skip push: latest QQQ data is {report.latest_date}, not current New York date.")
        return 0

    title, content = build_pushplus_message(report)
    pushplus_payload = build_pushplus_payload(pushplus_token, title, content)
    send_pushplus_message(pushplus_payload)
    print(f"Sent QQQ drawdown alert for {report.latest_date}.")
    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
