import argparse
import asyncio
import fcntl
import json
import logging
import os
import platform
import sqlite3
import sys
from contextlib import AsyncExitStack, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from vpn_payment.billing.models import BillingRules
from vpn_payment.blockchain.client import BaseClient, RPCError
from vpn_payment.blockchain.monitor import Monitor
from vpn_payment.blockchain.transactions import transaction_hash
from vpn_payment.bot.formatters import status_text
from vpn_payment.config import Settings, parse_time
from vpn_payment.database.repository import Repository, date
from vpn_payment.logging_setup import configure_logging
from vpn_payment.services.price_service import PriceService, PriceUnavailable
from vpn_payment.services.tracker import Tracker

log = logging.getLogger(__name__)


@contextmanager
def process_lock(database: Path):
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with database.with_suffix(database.suffix + ".lock").open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Другой процесс использует базу. Сначала остановите сервис бота."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Personal read-only Base ETH VPN payment tracker")
    commands = result.add_subparsers(dest="command")
    commands.add_parser("run", help="Start Telegram and background monitoring")
    commands.add_parser("status", help="Read saved balance without modifying the database")
    sync = commands.add_parser("sync", help="Discover and verify payments via RPC")
    sync.add_argument("--hash", action="append", default=[], dest="hashes")
    sync.add_argument("--hash-file", type=Path)
    imports = commands.add_parser("import-hashes", help="Import historical hashes via RPC")
    imports.add_argument("hashes", nargs="+")
    doctor = commands.add_parser("doctor", help="Check configuration without creating a database")
    doctor.add_argument("--online", action="store_true")
    doctor.add_argument("--at", type=parse_time, help="Historical quote timestamp with timezone")
    backup = commands.add_parser("backup", help="Back up an existing SQLite database")
    backup.add_argument("--output", type=Path)
    configure = commands.add_parser(
        "configure", help="Preview saved rules; --apply confirms changes"
    )
    configure.add_argument("--start", type=parse_time)
    configure.add_argument("--price", type=Decimal)
    configure.add_argument("--period-days", type=int)
    configure.add_argument("--notifications", choices=["on", "off"])
    configure.add_argument(
        "--acknowledge-history",
        action="store_true",
        help="Accept manually restored history as the basis for reminders, without claiming completeness",
    )
    configure.add_argument("--apply", action="store_true")
    adjust = commands.add_parser(
        "adjust", help="Preview a manual EUR estimate; --apply confirms it"
    )
    adjust.add_argument("tx_hash", type=transaction_hash)
    adjust.add_argument(
        "eur", help="Amount in EUR (for example 5.00), or auto to restore original estimate"
    )
    adjust.add_argument("--reason", required=True)
    adjust.add_argument("--apply", action="store_true")
    return result


async def run_bot(config: Settings, repo: Repository, tracker: Tracker, monitor: Monitor) -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.types import BotCommand, LinkPreviewOptions

    from vpn_payment.bot.handlers.menu import BotContext, create_router
    from vpn_payment.services.notifications import Notifications
    from vpn_payment.services.scheduler import notification_worker, sync_worker

    config.require_telegram()
    bot = Bot(
        config.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)
        ),
    )
    dispatcher = Dispatcher(disable_fsm=True)
    notifications = Notifications(config, repo, tracker, bot, monitor)
    dispatcher.include_router(create_router(BotContext(config, repo, tracker)))
    tasks = []
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Баланс VPN"),
                BotCommand(command="balance", description="Баланс"),
                BotCommand(command="history", description="Пополнения"),
            ]
        )
        tasks = [
            asyncio.create_task(sync_worker(config, monitor, notifications), name="sync"),
            asyncio.create_task(notification_worker(config, notifications), name="notifications"),
        ]
        log.info("Bot started; private owner-only access enabled")
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types(), close_bot_session=False
        )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dispatcher.storage.close()
        await bot.session.close()
        log.info("Bot stopped cleanly")


async def maintain(args, config: Settings, repo: Repository) -> int:
    actor = config.telegram_user_id or 0  # 0 identifies local CLI administration without Telegram.
    tracker = Tracker(config, repo)
    if args.command == "configure":
        current = await repo.settings()
        changes = {}
        if args.start is not None:
            changes.update(
                start_at=int(args.start.timestamp()), start_confirmed=1, billing_mode="continuous"
            )
        if args.price is not None:
            changes["price"] = str(args.price)
        if args.period_days is not None:
            changes["period_days"] = args.period_days
        if args.notifications is not None:
            changes["notifications_enabled"] = int(args.notifications == "on")
        candidate = {**current, **changes}
        if candidate.get("billing_mode", "prepaid") != "continuous":
            raise ValueError("Adopting legacy rules requires --start ISO_DATE")
        preview = await tracker.snapshot(
            rules=BillingRules(
                Decimal(candidate["price"]),
                candidate["period_days"],
                date(candidate["start_at"]),
                bool(candidate["start_confirmed"]),
            ),
        )
        print("Proposed changes:", json.dumps(changes, ensure_ascii=False))
        print(status_text(preview))
        if args.acknowledge_history:
            print("Accept known history for reminders; this does NOT prove its completeness.")
        if not changes and not args.acknowledge_history:
            print("Saved rules:", json.dumps(current))
            return 0
        if args.apply:
            await repo.backup(
                config.database_path.with_name(
                    config.database_path.stem
                    + datetime.now(UTC).strftime("-before-configure-%Y%m%dT%H%M%S%fZ.sqlite3")
                )
            )
            if changes:
                await repo.change_settings(changes, actor, current["revision"])
            if args.acknowledge_history:
                await repo.set_meta("history_ack_at", datetime.now(UTC).isoformat())
            print("Applied.")
        else:
            print("Preview only. Repeat with --apply to confirm.")
        return 0
    tx = await repo.transfer(args.tx_hash)
    if tx is None or tx["status"] != "confirmed":
        raise ValueError("Only an existing confirmed payment may be adjusted")
    try:
        amount = None if args.eur == "auto" else Decimal(args.eur)
    except InvalidOperation:
        raise ValueError("EUR must be a decimal amount or auto") from None
    if amount is not None and (
        not amount.is_finite() or not 0 <= amount <= 1_000_000 or amount.as_tuple().exponent < -2
    ):
        raise ValueError("Adjustment must be 0..1000000 EUR with at most two decimal places")
    if not 3 <= len(args.reason.strip()) <= 300:
        raise ValueError("Reason must contain 3..300 characters")
    preview = await tracker.snapshot(adjustment=(args.tx_hash, amount))
    print(
        "EUR:",
        tx["effective_eur"],
        "->",
        str(amount) if amount is not None else tx["estimated_eur"],
    )
    print("Reason:", args.reason.strip())
    print(status_text(preview))
    if args.apply:
        await repo.backup(
            config.database_path.with_name(
                config.database_path.stem
                + datetime.now(UTC).strftime("-before-adjust-%Y%m%dT%H%M%S%fZ.sqlite3")
            )
        )
        await repo.adjust(args.tx_hash, amount, args.reason, actor, tx["effective_eur"])
        print("Applied; original market estimate preserved.")
    else:
        print("Preview only. Repeat with --apply to confirm.")
    return 0


async def execute(args, config: Settings) -> int:
    command = args.command or "run"
    if command == "run":
        config.require_telegram()
    async with AsyncExitStack() as stack:
        if command == "doctor":
            print(
                f"Python {platform.python_version()}; SQLite {sqlite3.sqlite_version}; configuration OK; chain=8453"
            )
            print("Database exists:", config.database_path.is_file())
            if not args.online:
                return 0
            client, prices = BaseClient(config), PriceService(config, None)
            stack.push_async_callback(client.close)
            stack.push_async_callback(prices.close)
            code = 0
            try:
                print("Base RPC head:", await client.check())
            except RPCError as error:
                print(str(error))
                code = 1
            try:
                quote = await prices.at(args.at or config.history_start)
                print(
                    "Historical market quote (not a payment):",
                    json.dumps(asdict(quote), default=str),
                )
            except PriceUnavailable as error:
                print(str(error))
                code = 1
            return code
        maintenance = command in {"configure", "adjust"}
        if maintenance and not config.database_path.is_file():
            raise ValueError("Database does not exist; run sync first")
        repo = Repository(config.database_path)
        stack.push_async_callback(repo.close)
        await repo.open(
            config, readonly=command in {"status", "backup"} or (maintenance and not args.apply)
        )
        if command == "backup":
            destination = args.output or Path("backups") / datetime.now(UTC).strftime(
                "vpn-payment-%Y%m%dT%H%M%S%fZ.sqlite3"
            )
            print(await repo.backup(destination))
            return 0
        if maintenance:
            return await maintain(args, config, repo)
        tracker = Tracker(config, repo)
        if command == "status":
            print(status_text(await tracker.snapshot()))
            print("Last sync:", await repo.meta("last_success", "none"))
            print("Last error:", await repo.meta("last_error", "") or "none")
            return 0
        await repo.rules()  # Refuse implicit reinterpretation of legacy prepaid balances.
        client, prices = BaseClient(config), PriceService(config, repo)
        stack.push_async_callback(client.close)
        stack.push_async_callback(prices.close)
        monitor = Monitor(config, repo, client, prices)
        if command in {"sync", "import-hashes"}:
            hashes = list(args.hashes)
            if getattr(args, "hash_file", None):
                hashes.extend(args.hash_file.read_text(encoding="utf-8").replace(",", " ").split())
            report = await monitor.sync([transaction_hash(value) for value in hashes])
            print(report.text())
            return int(bool(report.errors))
        await run_bot(config, repo, tracker, monitor)
        return 0


def main() -> None:
    os.umask(0o077)
    args = parser().parse_args()
    try:
        config = Settings()
        configure_logging(config)
        if args.command in {"status", "backup", "doctor"}:
            code = asyncio.run(execute(args, config))
        else:
            with process_lock(config.database_path):
                code = asyncio.run(execute(args, config))
    except KeyboardInterrupt:
        code = 130
    except (ValueError, RPCError, PriceUnavailable) as error:
        log.error("%s", str(error))
        code = 1
    except Exception as error:
        log.error("Operation failed (%s)", type(error).__name__)
        code = 1
    sys.exit(code)
