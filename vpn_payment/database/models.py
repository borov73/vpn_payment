from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class VerifiedTransfer:
    tx_hash: str
    sender: str
    recipient: str
    value_wei: int
    block_number: int
    block_hash: str
    timestamp: datetime
    confirmations: int
    status: str = "confirmed"


@dataclass(frozen=True)
class Quote:
    exchange: str
    timestamp: datetime
    price: Decimal
    timeframe: str = "1m"
    price_field: str = "open"
    symbol: str = "ETH/EUR"


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quotes (
 id INTEGER PRIMARY KEY,
 exchange TEXT NOT NULL, symbol TEXT NOT NULL, candle_at INTEGER NOT NULL,
 timeframe TEXT NOT NULL, price_field TEXT NOT NULL, price TEXT NOT NULL,
 fetched_at INTEGER NOT NULL,
 UNIQUE(exchange, symbol, candle_at, timeframe, price_field)
);
CREATE TABLE IF NOT EXISTS transactions (
 tx_hash TEXT PRIMARY KEY,
 sender TEXT NOT NULL, recipient TEXT NOT NULL, value_wei TEXT NOT NULL,
 block_number INTEGER NOT NULL, block_hash TEXT NOT NULL, timestamp INTEGER NOT NULL,
 confirmations INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','confirmed','orphaned','failed','excluded')),
 quote_id INTEGER REFERENCES quotes(id), estimated_eur TEXT, raw_eur TEXT,
 price_error TEXT, discovered_at INTEGER NOT NULL, notify INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tx_time ON transactions(timestamp);
CREATE TABLE IF NOT EXISTS adjustments (
 id INTEGER PRIMARY KEY,
 tx_hash TEXT NOT NULL REFERENCES transactions(tx_hash), eur TEXT,
 reason TEXT NOT NULL, actor INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS adjustments_tx ON adjustments(tx_hash, id);
CREATE TABLE IF NOT EXISTS settings (
 id INTEGER PRIMARY KEY CHECK(id=1), price TEXT NOT NULL, period_days INTEGER NOT NULL,
 start_at INTEGER, start_confirmed INTEGER NOT NULL DEFAULT 0,
 notifications_enabled INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 billing_mode TEXT NOT NULL DEFAULT 'continuous' CHECK(billing_mode IN ('prepaid','continuous'))
);
CREATE TABLE IF NOT EXISTS settings_audit (
 id INTEGER PRIMARY KEY, change_json TEXT NOT NULL, actor INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_state (
 name TEXT PRIMARY KEY, start_block INTEGER NOT NULL, cursor INTEGER NOT NULL,
 cursor_hash TEXT NOT NULL, target INTEGER
);
CREATE TABLE IF NOT EXISTS notifications (
 key TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('reserved','sent','failed')),
 created_at INTEGER NOT NULL, sent_at INTEGER, message_id INTEGER, error TEXT,
 retry_at INTEGER
);
"""
