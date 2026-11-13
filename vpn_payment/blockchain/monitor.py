import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from vpn_payment.blockchain.client import BaseClient, MissingTransaction, RPCError, hexstr
from vpn_payment.blockchain.discovery import block_before, next_sender_block
from vpn_payment.blockchain.transactions import matches, transaction_hash, verify
from vpn_payment.config import Settings, address
from vpn_payment.database.repository import Repository
from vpn_payment.services.price_service import PriceService


@dataclass
class SyncReport:
    added: int = 0
    priced: int = 0
    busy: bool = False
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        if self.busy:
            return "Проверка уже выполняется. Повторная синхронизация не запущена."
        return "\n".join(
            [
                f"Новых платежей: {self.added}. Получено оценок EUR: {self.priced}.",
                *self.notes,
                *("⚠️ " + e for e in self.errors),
            ]
        )


class Monitor:
    def __init__(
        self,
        config: Settings,
        repo: Repository,
        client: BaseClient,
        prices: PriceService,
    ):
        self.config, self.repo, self.client, self.prices = config, repo, client, prices
        self.lock = asyncio.Lock()
        self.retired_limits: dict[str, int] = {}

    async def _eligible(self, recipient: str, block: int, tx_hash: str) -> bool:
        return (
            recipient in self.config.active_recipient_addresses
            or block <= self.retired_limits.get(recipient, -1)
            or await self.repo.transfer(tx_hash) is not None
        )

    async def _accept(self, tx_hash: str, head: int, notify: bool) -> bool:
        transfer = await verify(self.client, self.config, tx_hash, head)
        if transfer and not await self._eligible(
            transfer.recipient, transfer.block_number, tx_hash
        ):
            return False
        return await self.repo.save_transfer(transfer, notify) if transfer else False

    async def _reconcile(self, head: int, *, full: bool = False) -> list[str]:
        if full:
            # Persist before any RPC call, so an interruption cannot lose this audit.
            await self.repo.set_meta("reconcile_full_pending", "1")
        full = full or await self.repo.meta("reconcile_full_pending") == "1"
        errors = []
        for row in await self.repo.transfers():
            # Old confirmed ancestors are covered by the canonical scan checkpoints.
            # Do not make every new payment depend on fetching months-old receipts.
            if (
                not full
                and row["status"] != "pending"
                and row["block_number"] < head - 2 * self.config.confirmations + 1
            ):
                continue
            try:
                canonical = await self.client.block(row["block_number"])
                changed = hexstr(canonical["hash"]) != row["block_hash"]
                if changed:
                    await self.repo.set_status(row["tx_hash"], "orphaned")
                try:
                    tx = await verify(self.client, self.config, row["tx_hash"], head)
                except MissingTransaction:
                    if changed:
                        continue
                    raise
                if tx:
                    await self.repo.save_transfer(tx)
                else:
                    await self.repo.set_status(row["tx_hash"], "excluded")
            except RPCError as error:
                errors.append(str(error))
        await self.repo.set_meta("last_reconcile_error", "\n".join(errors))
        # A failed recent check must also be retried after the payment ages out.
        await self.repo.set_meta("reconcile_full_pending", "1" if errors else "0")
        return errors

    async def _initialize(self, target: int) -> None:
        current_recipients = ",".join(sorted(r.lower() for r in self.config.recipient_addresses))
        stored = await self.repo.meta("recipients")
        if stored and stored != current_recipients:
            await self.repo.set_meta("history_reset_pending", "1")
            await self.repo.set_meta("history_ack_at", "")
        await self.repo.set_meta("recipients", current_recipients)
        live = await self.repo.scan("live")
        if not live:
            block = await self.client.block(target)
            await self.repo.save_scan("live", target + 1, target, hexstr(block["hash"]))
            await self.repo.set_meta("bootstrap_anchor", str(target))
        if self.config.history_mode == "manual":
            return
        history = await self.repo.scan("history")
        reset_pending = await self.repo.meta("history_reset_pending") == "1"
        if not history or reset_pending:
            before = await block_before(self.client, self.config.history_start, target)
            block = await self.client.block(before)
            anchor = (
                target
                if reset_pending
                else int(await self.repo.meta("bootstrap_anchor", str(target)))
            )
            await self.repo.save_scan("history", before + 1, before, hexstr(block["hash"]), anchor)
            await self.repo.set_meta("history_reset_pending", "0")

    async def _scan(self, name: str, target: int, head: int, report: SyncReport) -> None:
        state = await self.repo.scan(name)
        if not state:
            return
        cursor = state["cursor"]
        current = await self.client.block(cursor)
        if hexstr(current["hash"]) != state["cursor_hash"]:
            # Include manually imported payments outside the automatic history scope.
            audit_errors = await self._reconcile(head, full=True)
            report.errors.extend(audit_errors)
            # Reset the finite scope instead of guessing a reorg depth/common ancestor.
            await self.repo.invalidate_from(state["start_block"])
            cursor = state["start_block"] - 1
            current = await self.client.block(cursor)
            await self.repo.save_scan(
                name, state["start_block"], cursor, hexstr(current["hash"]), state["target"]
            )
            if name == "live":
                await self.repo.set_meta("history_reset_pending", "1")
            report.notes.append(f"Реорганизация: диапазон {name} будет перепроверен.")
        if cursor >= target:
            return
        pinned_target = hexstr((await self.client.block(target))["hash"])
        mode = self.config.monitor_mode if name == "live" else "nonce"
        if mode == "nonce":
            for position in (cursor, target):
                code = bytes(await self.client.code(self.config.sender_address, position))
                if code and not code.startswith(b"\xef\x01\x00"):
                    raise RPCError(
                        "Nonce-поиск рассчитан на EOA, а не smart-contract wallet; используйте импорт хешей и blocks для мониторинга."
                    )
        limit = (
            self.config.max_tx_blocks_per_sync
            if mode == "nonce"
            else self.config.max_blocks_per_sync
        )
        for _ in range(limit):
            if cursor >= target:
                break
            found = (
                await next_sender_block(self.client, self.config.sender_address, cursor, target)
                if mode == "nonce"
                else cursor + 1
            )
            if found is None:
                checkpoint = await self.client.block(target)
                if hexstr(checkpoint["hash"]) != pinned_target:
                    raise RPCError("Подтверждённая вершина изменилась; диапазон будет повторён.")
                await self.repo.save_scan(
                    name, state["start_block"], target, hexstr(checkpoint["hash"]), state["target"]
                )
                cursor = target
                break
            block = await self.client.block(found, full=True)
            expected_hash = hexstr(block["hash"])
            for tx in block["transactions"]:
                if matches(tx, self.config) and await self._eligible(
                    address(tx["to"]), found, hexstr(tx["hash"])
                ):
                    report.added += int(
                        await self._accept(hexstr(tx["hash"]), head, name == "live")
                    )
            # Commit cursor only after every candidate's successful RPC verification.
            if hexstr((await self.client.block(found))["hash"]) != expected_hash:
                await self.repo.invalidate_from(found)
                raise RPCError("Блок изменился во время чтения; диапазон будет повторён.")
            if hexstr((await self.client.block(target))["hash"]) != pinned_target:
                await self.repo.invalidate_from(found)
                raise RPCError("Подтверждённая вершина изменилась; диапазон будет повторён.")
            cursor = found
            await self.repo.save_scan(
                name, state["start_block"], cursor, expected_hash, state["target"]
            )
        if cursor < target:
            report.notes.append(
                f"Диапазон {name}: проверено до {cursor}, цель {target}; продолжим со следующего блока."
            )

    async def history_description(self) -> str:
        history = await self.repo.scan("history")
        live = await self.repo.scan("live")
        lines = [f"История запрошена с {self.config.history_start:%Y-%m-%d %H:%M} UTC."]
        if self.config.history_mode == "manual":
            lines.append("Включён ручной импорт истории; автоматический nonce-поиск отключён.")
        if history:
            complete = (
                history["cursor"] >= history["target"]
                and await self.repo.meta("history_reset_pending") != "1"
            )
            lines.append(
                f"RPC-диапазон истории: {'проверен' if complete else 'НЕ ЗАВЕРШЁН'}; блок {history['cursor']} / {history['target']}."
            )
        else:
            lines.append("RPC-диапазон истории ещё не инициализирован; история неполна.")
        if live:
            lines.append(
                f"Мониторинг с блока {live['start_block']}; проверено до {live['cursor']}."
            )
        lines.append(
            "Переводы до HISTORY_START и internal transfers не входят в поиск. Импорт отдельных хешей не доказывает полноту истории."
        )
        return "\n".join(lines)

    async def sync(self, hashes: list[str] | None = None) -> SyncReport:
        if self.lock.locked():
            return SyncReport(busy=True)
        async with self.lock:
            report = SyncReport()
            await self.repo.set_meta("last_attempt", datetime.now(UTC).isoformat())
            try:
                head = await self.client.check()
                target = head - self.config.confirmations + 1
                if target < 1:
                    raise RPCError("В сети ещё нет достаточно подтверждённых блоков.")
                await self.repo.set_meta("last_confirmed_target", str(target))
                self.retired_limits = json.loads(await self.repo.meta("retired_recipients", "{}"))
                previous_limits = dict(self.retired_limits)
                for recipient in self.config.recipient_addresses:
                    if recipient not in self.config.active_recipient_addresses:
                        self.retired_limits.setdefault(recipient, head)
                if self.retired_limits != previous_limits:
                    await self.repo.set_meta("retired_recipients", json.dumps(self.retired_limits))
                audit_errors = await self._reconcile(head)
                report.errors.extend(audit_errors)
                if hashes:
                    for value in dict.fromkeys(hashes):
                        try:
                            report.added += int(
                                await self._accept(transaction_hash(value), head, False)
                            )
                        except (ValueError, RPCError) as error:
                            report.errors.append(str(error))
                # Live initialization is saved even if historical RPC state is unavailable.
                history_error = ""
                try:
                    await self._initialize(target)
                except RPCError as error:
                    report.errors.append(str(error))
                    history_error = str(error)
                try:
                    await self._scan("live", target, head, report)
                    await self.repo.set_meta("last_live_error", "")
                    await self.repo.set_meta("last_live_success", datetime.now(UTC).isoformat())
                except RPCError as error:
                    report.errors.append(str(error))
                    await self.repo.set_meta("last_live_error", str(error))
                history = await self.repo.scan("history")
                if history and self.config.history_mode == "nonce":
                    try:
                        await self._scan("history", history["target"], head, report)
                    except RPCError as error:
                        report.errors.append(str(error))
                        history_error = str(error)
                await self.repo.set_meta("last_history_error", history_error)
                report.priced = await self.prices.price_pending()
                unpriced = sum(
                    1
                    for row in await self.repo.transfers()
                    if row["status"] == "confirmed" and row["estimated_eur"] is None
                )
                if unpriced:
                    report.notes.append(
                        f"Платежей без исторического курса: {unpriced}. Они требуют уточнения; текущий курс не используется."
                    )
                report.notes.append(await self.history_description())
                if not report.errors:
                    await self.repo.set_meta("last_success", datetime.now(UTC).isoformat())
            except RPCError as error:
                report.errors.append(str(error))
                await self.repo.set_meta("last_live_error", str(error))
            await self.repo.set_meta("last_error", "\n".join(report.errors))
            return report
