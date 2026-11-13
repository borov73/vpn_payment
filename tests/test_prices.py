from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import ccxt.async_support as ccxt
import pytest

from tests.conftest import T0
from tests.test_blockchain import HASH
from tests.test_repository import transfer
from vpn_payment.database.models import Quote
from vpn_payment.services.price_service import PriceService, PriceUnavailable


def exchange(rows=None, error=None):
    return AsyncMock(
        load_markets=AsyncMock(return_value={"ETH/EUR": {"spot": True}}),
        fetch_ohlcv=AsyncMock(return_value=rows or [], side_effect=error),
        has={"fetchOHLCV": True},
        timeframes={"1m": "60"},
    )


def candle(at=T0, price="2500", volume="1"):
    return [int(at.timestamp()) * 1000, price, "2600", "2400", "2550", volume]


async def test_minute_open_exact_and_bounded_request(config, repo):
    ex = exchange([candle()])
    service = PriceService(config, repo, {"bitstamp": ex})
    quote = await service.at(T0 + timedelta(seconds=35), now=T0 + timedelta(minutes=1))
    assert quote.price == Decimal("2500") and quote.timestamp == T0
    assert quote.price_field == "open"
    ex.fetch_ohlcv.assert_awaited_once_with(
        "ETH/EUR",
        timeframe="1m",
        since=int(T0.timestamp()) * 1000,
        limit=2,
        params={"until": int(T0.timestamp()) * 1000 + 59999},
    )


async def test_fallback_when_no_trades_or_historical_gap(config, repo):
    first = exchange([candle(volume="0")])
    second = exchange([candle(price="2499.99")])
    service = PriceService(config, repo, {"bitstamp": first, "coinbaseexchange": second})
    quote = await service.at(T0)
    assert quote.exchange == "coinbaseexchange" and quote.price == Decimal("2499.99")


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [candle(T0 + timedelta(days=1))],
        [candle(price="NaN")],
        [candle(price="0")],
        [candle(price="-1")],
    ],
)
async def test_no_current_or_future_price_substitution(config, repo, rows):
    ex = exchange(rows)
    with pytest.raises(PriceUnavailable):
        await PriceService(config, repo, {"bitstamp": ex}).at(T0)
    ex.fetch_ticker.assert_not_called()


async def test_ccxt_errors_preserved_as_missing_and_retried(config, repo, monkeypatch):
    monkeypatch.setattr("vpn_payment.services.price_service.asyncio.sleep", AsyncMock())
    ex = exchange(error=ccxt.NetworkError("secret-url"))
    service = PriceService(config, repo, {"bitstamp": ex})
    await repo.save_transfer(transfer(config))
    assert await service.price_pending() == 0
    row = await repo.transfer(HASH)
    assert row["estimated_eur"] is None
    assert "NetworkError" in row["price_error"] and "secret-url" not in row["price_error"]
    assert ex.fetch_ohlcv.await_count == 2


async def test_missing_pair_uses_next_source(config, repo):
    first, second = exchange(), exchange([candle()])
    first.load_markets.return_value = {"ETH/USD": {"spot": True}}
    quote = await PriceService(config, repo, {"bitstamp": first, "coinbaseexchange": second}).at(T0)
    assert quote.exchange == "coinbaseexchange"
    first.fetch_ohlcv.assert_not_called()


async def test_unclosed_candle_not_used(config, repo):
    ex = exchange([candle()])
    with pytest.raises(PriceUnavailable, match="не закрыта"):
        await PriceService(config, repo, {"bitstamp": ex}).at(T0, now=T0 + timedelta(seconds=59))
    ex.load_markets.assert_not_called()


async def test_cached_source_works_offline(config, repo):
    await repo.save_transfer(transfer(config))
    await repo.apply_quote(HASH, Quote("bitstamp", T0, Decimal("2500")))
    ex = exchange(error=ccxt.ExchangeError("offline"))
    quote = await PriceService(config, repo, {"bitstamp": ex}).at(T0)
    assert quote.price == Decimal("2500")
    ex.load_markets.assert_not_called()
