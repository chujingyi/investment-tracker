"""Small adapter around the Longbridge SDK used by the web application."""

from decimal import Decimal
from datetime import datetime
import os
import time


class LongbridgeCapabilityUnavailable(RuntimeError):
    pass


class LongbridgeNotConfigured(RuntimeError):
    pass


def is_configured():
    required = (
        "LONGBRIDGE_APP_KEY",
        "LONGBRIDGE_APP_SECRET",
        "LONGBRIDGE_ACCESS_TOKEN",
    )
    return all(os.environ.get(name) for name in required)


def _number(value):
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _enum_value(value):
    """Return the stable text carried by SDK enums across SDK versions."""
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    text = str(raw)
    return text.rsplit(".", 1)[-1]


def _timestamp(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


class LongbridgeClient:
    """Expose only the quote and calendar operations needed by this app."""

    calendar_sync_delay = 1.1

    def __init__(self):
        if not is_configured():
            raise LongbridgeNotConfigured(
                "Longbridge API credentials are not configured on the server"
            )

        from service_support import configure_tls_trust
        configure_tls_trust()
        from longbridge.openapi import (
            AdjustType,
            Config,
            Language,
            Period,
            QuoteContext,
        )

        # Financial-calendar content should always be returned in Simplified Chinese.
        if hasattr(Config, "from_apikey"):
            config = Config.from_apikey(
                os.environ["LONGBRIDGE_APP_KEY"],
                os.environ["LONGBRIDGE_APP_SECRET"],
                os.environ["LONGBRIDGE_ACCESS_TOKEN"],
                language=Language.ZH_CN,
            )
        else:
            os.environ["LONGBRIDGE_LANGUAGE"] = "zh-CN"
            config = Config.from_env()
        self._quote = QuoteContext(config)
        self._config = config
        # CalendarContext only available in longbridge >= 4.x
        try:
            from longbridge.openapi import CalendarContext
            self._calendar = CalendarContext(config)
        except ImportError:
            self._calendar = None
        self._period_day = Period.Day
        self._no_adjust = AdjustType.NoAdjust

    def position_symbols(self):
        """Return symbols currently held across Longbridge stock accounts."""
        from longbridge.openapi import TradeContext

        response = TradeContext(self._config).stock_positions()
        symbols = []
        for channel in response.channels:
            for position in channel.positions:
                if _number(position.quantity) > 0 and position.symbol not in symbols:
                    symbols.append(position.symbol)
        return symbols

    def account_snapshot(self, start_at):
        """Return current positions and read-only execution history.

        Execution rows are enriched with the order side because some SDK
        releases omit ``side`` from the execution object itself.
        """
        from longbridge.openapi import TradeContext

        trade = TradeContext(self._config)
        position_response = trade.stock_positions()
        positions = []
        for channel in position_response.channels:
            for position in channel.positions:
                if _number(position.quantity) <= 0:
                    continue
                positions.append(
                    {
                        "symbol": position.symbol,
                        "name": getattr(position, "symbol_name", "") or "",
                        "quantity": _number(position.quantity),
                        "available_quantity": _number(position.available_quantity),
                        "cost_price": _number(position.cost_price),
                        "currency": str(position.currency or ""),
                        "market": _enum_value(position.market),
                        "last_price": None,
                    }
                )

        if positions:
            quote_map = {
                item["symbol"]: item["last"]
                for item in self.quotes([item["symbol"] for item in positions])
            }
            for position in positions:
                position["last_price"] = quote_map.get(position["symbol"])

        end_at = datetime.now()
        orders = list(trade.history_orders(start_at=start_at, end_at=end_at))
        executions = list(trade.history_executions(start_at=start_at, end_at=end_at))

        # Historical APIs exclude today's activity on some SDK versions.
        try:
            orders.extend(trade.today_orders())
            executions.extend(trade.today_executions())
        except Exception:
            pass

        order_sides = {
            str(order.order_id): _enum_value(getattr(order, "side", ""))
            for order in orders
        }
        rows = []
        seen = set()
        for execution in executions:
            order_id = str(getattr(execution, "order_id", "") or "")
            trade_id = str(getattr(execution, "trade_id", "") or "")
            happened_at = _timestamp(getattr(execution, "trade_done_at", ""))
            key = trade_id or "|".join(
                (
                    order_id,
                    execution.symbol,
                    happened_at,
                    str(execution.quantity),
                    str(execution.price),
                )
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "trade_key": key,
                    "trade_id": trade_id,
                    "order_id": order_id,
                    "symbol": execution.symbol,
                    "side": _enum_value(getattr(execution, "side", ""))
                    or order_sides.get(order_id, ""),
                    "quantity": _number(execution.quantity),
                    "price": _number(execution.price),
                    "trade_done_at": happened_at,
                }
            )

        return {"positions": positions, "executions": rows}

    def quotes(self, symbols):
        result = []
        for item in self._quote.quote(symbols):
            last = _number(item.last_done)
            previous = _number(
                getattr(item, "prev_close_price", None)
                or getattr(item, "prev_close", None)
            )
            change_percentage = (
                (last - previous) / previous * 100 if previous else 0.0
            )
            result.append(
                {
                    "symbol": item.symbol,
                    "last": last,
                    "prev_close": previous,
                    "open": _number(item.open),
                    "high": _number(item.high),
                    "low": _number(item.low),
                    "volume": int(item.volume or 0),
                    "turnover": _number(item.turnover),
                    "change_percentage": change_percentage,
                }
            )
        return result

    def candlesticks(self, symbol, count):
        candles = self._quote.candlesticks(
            symbol, self._period_day, count, self._no_adjust
        )
        return [
            {
                "timestamp": candle.timestamp,
                "open": _number(candle.open),
                "high": _number(candle.high),
                "low": _number(candle.low),
                "close": _number(candle.close),
                "volume": int(candle.volume or 0),
                "turnover": _number(candle.turnover),
            }
            for candle in candles
        ]

    def calendar(self, category, start, end, market=None, symbols=None):
        if self._calendar is None:
            raise LongbridgeCapabilityUnavailable("calendar capability unavailable")

        from longbridge.openapi import CalendarCategory

        categories = {
            "report": CalendarCategory.Report,
            "dividend": CalendarCategory.Dividend,
            "split": CalendarCategory.Split,
            "ipo": CalendarCategory.Ipo,
            "macrodata": CalendarCategory.MacroData,
            "closed": CalendarCategory.Closed,
        }
        wanted = set(symbols or [])
        groups = []
        cursor = start

        # The endpoint returns a pagination cursor in next_date.
        for _ in range(24):
            for attempt in range(3):
                try:
                    response = self._calendar.finance_calendar(
                        categories[category], cursor, end, market
                    )
                    break
                except Exception as exc:
                    if "429002" not in str(exc) or attempt == 2:
                        raise
                    time.sleep(1.5 * (attempt + 1))
            for group in response.list:
                infos = []
                for info in group.infos:
                    if wanted and info.symbol not in wanted:
                        continue
                    infos.append(
                        {
                            "id": str(getattr(info, "id", "") or ""),
                            "symbol": info.symbol,
                            "market": info.market,
                            "counter_name": info.counter_name,
                            "content": info.content,
                            "date": info.date,
                            "date_type": info.date_type,
                            "event_type": info.event_type,
                            "star": int(getattr(info, "star", 0) or 0),
                            "data": {
                                (value.key or value.value_type): value.value
                                for value in (info.data_kv or [])
                            },
                        }
                    )
                if infos:
                    groups.append({"date": group.date, "infos": infos})

            # Some deployed SDK builds omit next_date even though newer stubs expose it.
            next_date = getattr(response, "next_date", "") or ""
            if not next_date or next_date <= cursor or next_date > end:
                break
            cursor = next_date

        return {"list": groups}
