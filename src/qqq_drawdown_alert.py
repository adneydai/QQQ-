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
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
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


def parse_yahoo_chart(payload: dict[str, Any]) -> list[dict[str, Any]]:
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        raise MarketDataError("Yahoo payload is missing chart data")

    error = chart.get("error")
    if error:
        if isinstance(error, dict):
            raise MarketDataError(str(error.get("description") or error))
        raise MarketDataError(str(error))

    results = chart.get("result")
    if not results:
        raise MarketDataError("Yahoo payload is missing chart result")

    result = results[0]
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") or []
    closes = quotes[0].get("close") if quotes else None
    if not timestamps or not closes:
        raise MarketDataError("Yahoo payload is missing daily close prices")

    bars = []
    for timestamp, close in zip(timestamps, closes):
        if close is None:
            continue
        bar_date = dt.datetime.fromtimestamp(timestamp, tz=NEW_YORK_TZ).date()
        bars.append({"date": bar_date, "close": float(close)})

    if not bars:
        raise MarketDataError("Yahoo daily time series is empty")

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


def fetch_yahoo_chart(symbol: str = SYMBOL) -> dict[str, Any]:
    query = parse.urlencode({"range": "1y", "interval": "1d", "events": "history"})
    url = f"{YAHOO_CHART_URL}/{parse.quote(symbol)}?{query}"
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
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
    pushplus_token = os.environ.get("PUSHPLUS_TOKEN")
    if not pushplus_token:
        raise RuntimeError("PUSHPLUS_TOKEN is not set")

    market_payload = fetch_yahoo_chart()
    bars = parse_yahoo_chart(market_payload)
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
