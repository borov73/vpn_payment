import logging
import math
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, Message

from vpn_payment.bot.formatters import history_text, status_text
from vpn_payment.bot.keyboards import history_menu, menu
from vpn_payment.bot.middlewares.auth import OwnerOnly
from vpn_payment.config import Settings
from vpn_payment.database.repository import Repository
from vpn_payment.services.tracker import Tracker

log = logging.getLogger(__name__)
PAGE_SIZE = 5


@dataclass
class BotContext:
    config: Settings
    repo: Repository
    tracker: Tracker


def create_router(ctx: BotContext) -> Router:
    router = Router(name="personal-vpn")
    auth = OwnerOnly(ctx.config.telegram_user_id)
    router.message.outer_middleware(auth)
    router.callback_query.outer_middleware(auth)

    async def render(message: Message, text: str, markup, edit: bool = False):
        if edit:
            try:
                await message.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as error:
                if "message is not modified" in error.message.lower():
                    return
        await message.answer(text, reply_markup=markup)

    async def balance(message: Message, edit: bool = False):
        snapshot = await ctx.tracker.snapshot()
        text = status_text(snapshot)
        if (
            await ctx.repo.meta("last_live_error", "")
            or await ctx.repo.meta("last_reconcile_error", "")
            or await ctx.repo.meta("reconcile_full_pending") == "1"
        ):
            text += "\n\n⏳ Данные пока не обновились. Проверю ещё раз автоматически."
        else:
            live = await ctx.repo.scan("live")
            target = await ctx.repo.meta("last_confirmed_target")
            history = await ctx.repo.scan("history")
            acknowledged = bool(await ctx.repo.meta("history_ack_at"))
            incomplete = (
                not history
                or history["cursor"] < history["target"]
                or await ctx.repo.meta("history_reset_pending") == "1"
            )
            if (
                not live
                or (target and live["cursor"] < int(target))
                or (incomplete and not acknowledged)
            ):
                text += "\n\n⏳ Проверяю пополнения; баланс может обновиться."
        await render(message, text, menu(), edit)

    async def history(message: Message, page: int, edit: bool = False):
        rows = [row for row in await ctx.tracker.transfers() if row["status"] == "confirmed"]
        pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
        page = max(0, min(page, pages - 1))
        selected = rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        await render(message, history_text(selected, page, pages), history_menu(page, pages), edit)

    @router.message(CommandStart())
    @router.message(Command("balance", "status"))
    async def show_balance(message: Message):
        await balance(message)

    @router.message(Command("history"))
    async def show_history(message: Message):
        await history(message, 0)

    @router.callback_query(F.data.in_({"menu", "balance", "status"}))
    async def balance_callback(query: CallbackQuery):
        await query.answer()
        if isinstance(query.message, Message):
            await balance(query.message, edit=True)

    @router.callback_query(F.data.regexp(r"^history:\d+$"))
    async def history_callback(query: CallbackQuery):
        await query.answer()
        if isinstance(query.message, Message):
            await history(query.message, int(query.data.split(":")[1]), edit=True)

    @router.callback_query()
    async def old_callback(query: CallbackQuery):
        # Previous settings and confirmation buttons must never change the ledger.
        await query.answer("Меню обновлено")
        if isinstance(query.message, Message):
            await balance(query.message, edit=True)

    @router.message()
    async def fallback(message: Message):
        await balance(message)

    @router.errors()
    async def handler_error(event: ErrorEvent):
        log.error("Bot handler failed (%s)", type(event.exception).__name__)
        message = event.update.message or (
            event.update.callback_query.message if event.update.callback_query else None
        )
        if isinstance(message, Message):
            try:
                await message.answer(
                    "⏳ Не получилось обновить данные. Попробуй чуть позже.", reply_markup=menu()
                )
            except Exception:
                log.warning("Unable to report handler failure")
        return True

    return router
