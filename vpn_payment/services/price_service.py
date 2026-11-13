import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import ccxt.async_support as ccxt

from vpn_payment.config import Settings, utc
from vpn_payment.database.models import Quote
from vpn_payment.database.repository import Repository


class PriceUnavailable(RuntimeError):
    pass


class PriceService:
    """Minute OPEN of the closed candle containing the block timestamp.

    Requests are tiny, explicitly bounded historical windows. No ticker/current-price
    fallback, no FX cross-rate, no forward candle, and no unbounded auto-pagination.
    """

    def __init__(self, config: Settings, repo: Repository | None, exchanges: dict | None = None):
        self.config, self.repo = config, repo
        self.exchanges = (
            exchanges
            if exchanges is not None
            else {
                name: getattr(ccxt, name)(
                    {"enableRateLimit": True, "timeout": config.price_timeout_seconds * 1000}
                )
                for name in config.price_exchanges
            }
        )
        for exchange in self.exchanges.values():
            # CCXT's parser can return decimal values without binary float arithmetic.
            exchange.number = lambda value: Decimal(str(value))
        self.lock = asyncio.Lock()

    async def at(self, timestamp: datetime, now: datetime | None = None) -> Quote:
        moment = utc(timestamp)
        candle = moment.replace(second=0, microsecond=0)
        if candle + timedelta(minutes=1) > utc(now or datetime.now(UTC)):
            raise PriceUnavailable("Минутная свеча ещё не закрыта; оценка будет повторена.")
        errors: list[str] = []
        async with self.lock:
            for name, exchange in self.exchanges.items():
                cached = await self.repo.quote(name, candle) if self.repo else None
                if cached:
                    return cached
                for attempt in range(2):
                    try:
                        markets = await exchange.load_markets()
                        if (
                            "ETH/EUR" not in markets
                            or not exchange.has.get("fetchOHLCV")
                            or "1m" not in exchange.timeframes
                            or markets["ETH/EUR"].get("spot") is False
                        ):
                            errors.append(f"{name}: ETH/EUR 1m не поддерживается")
                            break
                        start = int(candle.timestamp()) * 1000
                        rows = await exchange.fetch_ohlcv(
                            "ETH/EUR",
                            timeframe="1m",
                            since=start,
                            limit=2,
                            params={"until": start + 59_999},
                        )
                        for row in rows:
                            if len(row) < 6 or int(row[0]) != start:
                                continue
                            price, volume = Decimal(str(row[1])), Decimal(str(row[5]))
                            if (
                                price.is_finite()
                                and price > 0
                                and volume.is_finite()
                                and volume > 0
                            ):
                                return Quote(name, candle, price)
                        errors.append(f"{name}: отсутствует свеча с торгами за нужную минуту")
                        break
                    except (
                        ccxt.BaseError,
                        TimeoutError,
                        OSError,
                        ValueError,
                        TypeError,
                        InvalidOperation,
                    ) as error:
                        if attempt == 0 and isinstance(
                            error, (ccxt.NetworkError, TimeoutError, OSError)
                        ):
                            await asyncio.sleep(0.5)
                            continue
                        errors.append(f"{name}: {type(error).__name__}")
                        break
        raise PriceUnavailable("Исторический ETH/EUR недоступен. " + "; ".join(errors))

    async def price_pending(self) -> int:
        if self.repo is None:
            raise ValueError("Pricing saved payments requires a repository")
        updated = 0
        for tx in await self.repo.transfers():
            if tx["status"] != "confirmed" or tx["estimated_eur"] is not None:
                continue
            try:
                quote = await self.at(datetime.fromtimestamp(tx["timestamp"], UTC))
                await self.repo.apply_quote(tx["tx_hash"], quote)
                updated += 1
            except PriceUnavailable as error:
                await self.repo.price_error(tx["tx_hash"], str(error))
        return updated

    async def close(self) -> None:
        for exchange in self.exchanges.values():
            await exchange.close()
