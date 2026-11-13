import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path

import aiosqlite

from vpn_payment.billing.models import BillingRules, money
from vpn_payment.config import Settings, utc
from vpn_payment.database.models import SCHEMA, Quote, VerifiedTransfer


def stamp(value: datetime) -> int:
    return int(utc(value).timestamp())


def date(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value is not None else None


def now_stamp() -> int:
    return int(datetime.now(UTC).timestamp())


class StaleChange(ValueError):
    pass


class Repository:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.lock = asyncio.Lock()
        self.db: aiosqlite.Connection | None = None

    async def open(self, config: Settings, *, readonly: bool = False) -> None:
        if readonly and not self.path.is_file():
            raise ValueError("Database does not exist; refusing to create an empty source")
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        source = self.path.resolve().as_uri() + "?mode=ro" if readonly else self.path
        self.db = await aiosqlite.connect(source, uri=readonly)
        self.db.row_factory = aiosqlite.Row
        try:
            await self.db.execute("PRAGMA busy_timeout=5000")
            version = (await (await self.db.execute("PRAGMA user_version")).fetchone())[0]
            if version not in (0, 1, 2, 3):
                raise ValueError("Database schema is newer than this application; do not downgrade")
            if readonly:
                if version == 0:
                    raise ValueError("Database is not initialized; run sync first")
                return
            os.chmod(self.path, 0o600)
            if version in (1, 2):
                await self.backup(
                    self.path.with_name(
                        self.path.stem
                        + datetime.now(UTC).strftime("-before-v3-%Y%m%dT%H%M%S%fZ.sqlite3")
                    )
                )
            await self.db.execute("PRAGMA journal_mode=WAL")
            await self.db.execute("PRAGMA foreign_keys=ON")
            await self.db.executescript(SCHEMA)
            columns = {row["name"] for row in await self.rows("PRAGMA table_info(settings)")}
            if "billing_mode" not in columns:
                # Preserve the meaning of legacy data until explicitly adopted via configure.
                await self.db.execute(
                    "ALTER TABLE settings ADD COLUMN billing_mode TEXT NOT NULL DEFAULT 'prepaid'"
                )
            notification_columns = {
                row["name"] for row in await self.rows("PRAGMA table_info(notifications)")
            }
            if "retry_at" not in notification_columns:
                await self.db.execute("ALTER TABLE notifications ADD COLUMN retry_at INTEGER")
            await self.db.execute(
                "INSERT OR IGNORE INTO settings(id,price,period_days,start_at,start_confirmed,notifications_enabled,billing_mode) VALUES(1,?,?,?,?,?,'continuous')",
                (
                    str(config.monthly_price_eur),
                    config.period_days,
                    stamp(config.subscription_start) if config.subscription_start else None,
                    int(config.subscription_start is not None),
                    int(config.notifications_enabled),
                ),
            )
            identity = json.dumps(
                [8453, config.sender_address.lower(), config.history_start.isoformat()]
            )
            row = await (
                await self.db.execute("SELECT value FROM meta WHERE key='identity'")
            ).fetchone()
            if row and row[0] != identity:
                raise ValueError("Wallet/HISTORY_START changed: use a separate DATABASE_PATH")
            await self.db.execute("INSERT OR IGNORE INTO meta VALUES('identity',?)", (identity,))
            await self.db.execute("PRAGMA user_version=3")
            await self.db.commit()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    async def rows(self, sql: str, args=()) -> list[dict]:
        async with self.lock:
            async with self.db.execute(sql, args) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def meta(self, key: str, default: str | None = None) -> str | None:
        rows = await self.rows("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    async def set_meta(self, key: str, value: str) -> None:
        async with self.lock:
            await self.db.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await self.db.commit()

    async def settings(self) -> dict:
        return (await self.rows("SELECT * FROM settings WHERE id=1"))[0]

    async def rules(self) -> BillingRules:
        row = await self.settings()
        if row.get("billing_mode", "prepaid") != "continuous":
            raise ValueError(
                "Legacy prepaid rules require explicit adoption: configure --start ISO_DATE --apply"
            )
        return BillingRules(
            Decimal(row["price"]),
            row["period_days"],
            date(row["start_at"]),
            bool(row["start_confirmed"]),
        )

    async def change_settings(self, changes: dict, actor: int, expected_revision: int) -> None:
        allowed = {
            "price",
            "period_days",
            "start_at",
            "start_confirmed",
            "notifications_enabled",
            "billing_mode",
        }
        if not changes or not set(changes) <= allowed:
            raise ValueError("Unsupported settings")
        if "price" in changes:
            p = Decimal(changes["price"])
            if not p.is_finite() or p <= 0 or p > 10000 or money(p) != p:
                raise ValueError("Price must be 0.01..10000 EUR with at most two decimal places")
            changes["price"] = str(p)
        if "period_days" in changes and not 1 <= int(changes["period_days"]) <= 366:
            raise ValueError("Period must be 1..366 days")
        if "billing_mode" in changes and changes["billing_mode"] != "continuous":
            raise ValueError("Only continuous bookkeeping is supported")
        async with self.lock:
            current = await (
                await self.db.execute("SELECT revision FROM settings WHERE id=1")
            ).fetchone()
            if current[0] != expected_revision:
                raise StaleChange("Настройки уже изменились. Откройте их заново.")
            assignments = ",".join(f"{key}=?" for key in changes)
            try:
                await self.db.execute(
                    f"UPDATE settings SET {assignments},revision=revision+1 WHERE id=1",
                    tuple(changes.values()),
                )
                await self.db.execute(
                    "INSERT INTO settings_audit(change_json,actor,created_at) VALUES(?,?,?)",
                    (json.dumps(changes), actor, now_stamp()),
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def save_transfer(self, tx: VerifiedTransfer, notify: bool = False) -> bool:
        async with self.lock:
            old = await (
                await self.db.execute("SELECT * FROM transactions WHERE tx_hash=?", (tx.tx_hash,))
            ).fetchone()
            if old:
                changed = old["block_hash"] != tx.block_hash or old["timestamp"] != stamp(
                    tx.timestamp
                )
                # A re-included transaction may have a new timestamp: its old quote no longer applies.
                await self.db.execute(
                    "UPDATE transactions SET block_number=?,block_hash=?,timestamp=?,confirmations=?,status=?,recipient=?,notify=MAX(notify,?) WHERE tx_hash=?",
                    (
                        tx.block_number,
                        tx.block_hash,
                        stamp(tx.timestamp),
                        tx.confirmations,
                        tx.status,
                        tx.recipient,
                        int(notify),
                        tx.tx_hash,
                    ),
                )
                if changed:
                    await self.db.execute(
                        "UPDATE transactions SET quote_id=NULL,estimated_eur=NULL,raw_eur=NULL WHERE tx_hash=?",
                        (tx.tx_hash,),
                    )
            else:
                await self.db.execute(
                    "INSERT INTO transactions(tx_hash,sender,recipient,value_wei,block_number,block_hash,timestamp,confirmations,status,discovered_at,notify) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tx.tx_hash,
                        tx.sender,
                        tx.recipient,
                        str(tx.value_wei),
                        tx.block_number,
                        tx.block_hash,
                        stamp(tx.timestamp),
                        tx.confirmations,
                        tx.status,
                        now_stamp(),
                        int(notify),
                    ),
                )
            await self.db.commit()
            return old is None

    async def set_status(self, tx_hash: str, status: str) -> None:
        async with self.lock:
            await self.db.execute(
                "UPDATE transactions SET status=? WHERE tx_hash=?", (status, tx_hash)
            )
            await self.db.commit()

    async def transfers(self) -> list[dict]:
        sql = """SELECT t.*, q.exchange, q.symbol, q.candle_at, q.timeframe, q.price_field, q.price,
          (SELECT a.eur FROM adjustments a WHERE a.tx_hash=t.tx_hash ORDER BY a.id DESC LIMIT 1) AS adjusted_eur,
          (SELECT a.reason FROM adjustments a WHERE a.tx_hash=t.tx_hash ORDER BY a.id DESC LIMIT 1) AS adjustment_reason,
          COALESCE((SELECT a.eur FROM adjustments a WHERE a.tx_hash=t.tx_hash ORDER BY a.id DESC LIMIT 1),t.estimated_eur) AS effective_eur
          FROM transactions t LEFT JOIN quotes q ON q.id=t.quote_id ORDER BY t.timestamp DESC,t.tx_hash"""
        return await self.rows(sql)

    async def transfer(self, tx_hash: str) -> dict | None:
        # A personal wallet normally has only a handful of VPN payments.
        return next((row for row in await self.transfers() if row["tx_hash"] == tx_hash), None)

    async def quote(self, exchange: str, at: datetime) -> Quote | None:
        rows = await self.rows(
            "SELECT * FROM quotes WHERE exchange=? AND symbol='ETH/EUR' AND candle_at=? AND timeframe='1m' AND price_field='open'",
            (exchange, stamp(at)),
        )
        return (
            Quote(exchange, date(rows[0]["candle_at"]), Decimal(rows[0]["price"])) if rows else None
        )

    async def apply_quote(self, tx_hash: str, quote: Quote) -> None:
        if not quote.price.is_finite() or quote.price <= 0:
            raise ValueError("Invalid quote")
        async with self.lock:
            try:
                await self.db.execute(
                    "INSERT OR IGNORE INTO quotes(exchange,symbol,candle_at,timeframe,price_field,price,fetched_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        quote.exchange,
                        quote.symbol,
                        stamp(quote.timestamp),
                        quote.timeframe,
                        quote.price_field,
                        str(quote.price),
                        now_stamp(),
                    ),
                )
                cached = await (
                    await self.db.execute(
                        "SELECT id,price FROM quotes WHERE exchange=? AND symbol=? AND candle_at=? AND timeframe=? AND price_field=?",
                        (
                            quote.exchange,
                            quote.symbol,
                            stamp(quote.timestamp),
                            quote.timeframe,
                            quote.price_field,
                        ),
                    )
                ).fetchone()
                tx = await (
                    await self.db.execute(
                        "SELECT value_wei,timestamp FROM transactions WHERE tx_hash=?", (tx_hash,)
                    )
                ).fetchone()
                if (
                    not tx
                    or not stamp(quote.timestamp) <= tx["timestamp"] < stamp(quote.timestamp) + 60
                ):
                    raise ValueError("Quote must contain the transaction's minute")
                with localcontext() as ctx:
                    ctx.prec = 60
                    raw = Decimal(tx["value_wei"]) / Decimal(10**18) * Decimal(cached["price"])
                    eur = money(raw)
                # Never overwrite the original estimate on a price refresh.
                await self.db.execute(
                    "UPDATE transactions SET quote_id=?,estimated_eur=?,raw_eur=?,price_error=NULL WHERE tx_hash=? AND estimated_eur IS NULL",
                    (cached["id"], str(eur), str(raw), tx_hash),
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def price_error(self, tx_hash: str, error: str) -> None:
        async with self.lock:
            await self.db.execute(
                "UPDATE transactions SET price_error=? WHERE tx_hash=?", (error, tx_hash)
            )
            await self.db.commit()

    async def adjust(
        self, tx_hash: str, eur: Decimal | None, reason: str, actor: int, expected_eur: str | None
    ) -> None:
        if eur is not None and (
            not eur.is_finite() or not 0 <= eur <= 1_000_000 or money(eur) != eur
        ):
            raise ValueError("Adjustment must be 0..1000000 EUR with at most two decimal places")
        if not 3 <= len(reason.strip()) <= 300:
            raise ValueError("Reason must contain 3..300 characters")
        async with self.lock:
            row = await (
                await self.db.execute(
                    "SELECT COALESCE((SELECT eur FROM adjustments WHERE tx_hash=t.tx_hash ORDER BY id DESC LIMIT 1),estimated_eur),status FROM transactions t WHERE tx_hash=?",
                    (tx_hash,),
                )
            ).fetchone()
            if not row or row[1] != "confirmed":
                raise ValueError("Only an existing confirmed payment may be adjusted")
            if row[0] != expected_eur:
                raise StaleChange("Оценка платежа изменилась. Откройте корректировку заново.")
            await self.db.execute(
                "INSERT INTO adjustments(tx_hash,eur,reason,actor,created_at) VALUES(?,?,?,?,?)",
                (
                    tx_hash,
                    str(eur) if eur is not None else None,
                    reason.strip(),
                    actor,
                    now_stamp(),
                ),
            )
            await self.db.commit()

    async def scan(self, name: str) -> dict | None:
        rows = await self.rows("SELECT * FROM scan_state WHERE name=?", (name,))
        return rows[0] if rows else None

    async def save_scan(
        self, name: str, start_block: int, cursor: int, cursor_hash: str, target: int | None = None
    ) -> None:
        async with self.lock:
            await self.db.execute(
                "INSERT INTO scan_state VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET start_block=excluded.start_block,cursor=excluded.cursor,cursor_hash=excluded.cursor_hash,target=excluded.target",
                (name, start_block, cursor, cursor_hash, target),
            )
            await self.db.commit()

    async def invalidate_from(self, block: int) -> None:
        async with self.lock:
            await self.db.execute(
                "UPDATE transactions SET status='orphaned' WHERE block_number>=?", (block,)
            )
            await self.db.commit()

    async def payment_notifications(self, at: datetime) -> set[str]:
        rows = await self.rows(
            """SELECT t.tx_hash FROM transactions t
               LEFT JOIN notifications n ON n.key='payment:' || t.tx_hash
               WHERE t.notify=1 AND t.status='confirmed' AND t.timestamp<=?
               AND (n.key IS NULL OR (n.state='failed' AND n.retry_at<=?))""",
            (stamp(at), stamp(at)),
        )
        return {row["tx_hash"] for row in rows}

    async def reserve_notification(self, key: str, at: datetime | None = None) -> bool:
        now = stamp(at) if at else now_stamp()
        async with self.lock:
            cursor = await self.db.execute(
                """INSERT INTO notifications(key,state,created_at) VALUES(?,'reserved',?)
                   ON CONFLICT(key) DO UPDATE SET state='reserved',retry_at=NULL,error=NULL
                   WHERE notifications.state='failed' AND notifications.retry_at<=?""",
                (key, now, now),
            )
            await self.db.commit()
            return cursor.rowcount == 1

    async def finish_notification(
        self,
        key: str,
        message_id: int | None,
        error: str | None = None,
        retry_at: int | None = None,
    ) -> None:
        async with self.lock:
            await self.db.execute(
                "UPDATE notifications SET state=?,sent_at=?,message_id=?,error=?,retry_at=? WHERE key=?",
                (
                    "failed" if error else "sent",
                    now_stamp() if not error else None,
                    message_id,
                    error,
                    retry_at,
                    key,
                ),
            )
            await self.db.commit()

    async def backup(self, destination: Path) -> Path:
        if await asyncio.to_thread(destination.resolve) == await asyncio.to_thread(
            self.path.resolve
        ):
            raise ValueError("Backup must use a different path")
        if await asyncio.to_thread(destination.exists):
            raise ValueError("Refusing to overwrite an existing backup")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            raise ValueError("Refusing to overwrite an existing backup") from None
        async with self.lock:
            async with aiosqlite.connect(destination) as target:
                await self.db.backup(target)
                result = await (await target.execute("PRAGMA integrity_check")).fetchone()
                if result[0] != "ok":
                    raise ValueError("Backup integrity check failed")
        os.chmod(destination, 0o600)
        return destination
