from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from tests.conftest import T0
from tests.test_repository import valued
from vpn_payment.bot.handlers.menu import BotContext, create_router
from vpn_payment.bot.keyboards import history_menu, menu
from vpn_payment.services.tracker import Tracker


class FakeTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 - aiogram interface
        self.sent.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(
                message_id=len(self.sent),
                date=T0,
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        return True

    async def stream_content(self, *args, **kwargs) -> AsyncGenerator[bytes, None]:
        if False:
            yield b""


@pytest.fixture
async def ui(config, repo):
    config = config.model_copy(update={"telegram_user_id": 42})
    session = FakeTelegram()
    bot = Bot(token="42:TEST_ONLY", session=session)
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.include_router(create_router(BotContext(config, repo, Tracker(config, repo))))
    sequence = 0

    async def send(text=None, callback=None, user=42, chat_type="private"):
        nonlocal sequence
        sequence += 1
        person = User(id=user, is_bot=False, first_name="Test")
        message = Message(
            message_id=sequence,
            date=T0,
            chat=Chat(id=user, type=chat_type),
            from_user=person,
            text=text,
        )
        update = (
            Update(update_id=sequence, message=message)
            if callback is None
            else Update(
                update_id=sequence,
                callback_query=CallbackQuery(
                    id=str(sequence),
                    from_user=person,
                    chat_instance="test",
                    message=message,
                    data=callback,
                ),
            )
        )
        await dispatcher.feed_update(bot, update)
        return [item for item in session.sent if isinstance(item, (SendMessage, EditMessageText))]

    yield SimpleNamespace(send=send, session=session, config=config)
    await dispatcher.storage.close()
    await bot.session.close()


async def test_owner_sees_only_two_buttons_and_others_are_blocked(ui):
    sent = await ui.send("/start")
    buttons = [b.text for row in sent[-1].reply_markup.inline_keyboard for b in row]
    assert buttons == ["💰 Баланс", "📜 Пополнения"]
    assert "Баланс VPN" in sent[-1].text
    sent = await ui.send("/balance", user=99)
    assert sent[-1].text == "Доступ запрещён."
    previous = len(sent)
    await ui.send(callback="history:0", user=99)
    assert isinstance(ui.session.sent[-1], AnswerCallbackQuery)
    assert ui.session.sent[-1].show_alert
    assert (
        len([x for x in ui.session.sent if isinstance(x, (SendMessage, EditMessageText))])
        == previous
    )


async def test_group_access_is_blocked(ui):
    assert (await ui.send("/balance", chat_type="group"))[-1].text == "Доступ запрещён."


async def test_history_displays_only_dates_and_eur_and_edits_in_place(ui, repo):
    await valued(repo, ui.config)
    sent = await ui.send(callback="history:0")
    card = sent[-1]
    assert isinstance(card, EditMessageText)
    assert "01.08.2026" in card.text and "+5,00 €" in card.text
    assert "ETH" not in card.text and "0x" not in card.text and "bitstamp" not in card.text
    assert "BaseScan" not in card.text
    assert len(card.text) < 200
    sent = await ui.send(callback="balance")
    assert isinstance(sent[-1], EditMessageText)
    assert "Баланс VPN" in sent[-1].text


@pytest.mark.parametrize(
    "callback",
    [
        "settings",
        "stats",
        "sync",
        "notifications",
        "edit:price",
        "adjust:1",
        "review_mode:continuous",
        "confirm_start",
        "apply:1234abcd",
        "import",
        "clear_restarts",
    ],
)
async def test_old_buttons_never_change_settings_or_payments(ui, repo, callback):
    await valued(repo, ui.config)
    settings = await repo.settings()
    payments = await repo.transfers()
    sent = await ui.send(callback=callback)
    assert "Баланс VPN" in sent[-1].text
    assert await repo.settings() == settings and await repo.transfers() == payments
    assert not await repo.rows("SELECT * FROM settings_audit")
    assert not await repo.rows("SELECT * FROM adjustments")


@pytest.mark.parametrize("command", ["/settings", "/stats", "/import", "/cancel", "hello"])
async def test_removed_commands_return_simple_balance(ui, repo, command):
    sent = await ui.send(command)
    assert "Баланс VPN" in sent[-1].text
    assert len(sent[-1].reply_markup.inline_keyboard) == 1
    assert not await repo.rows("SELECT * FROM settings_audit")


async def test_history_pagination_and_clamping(ui, repo):
    from datetime import timedelta
    from decimal import Decimal

    from tests.test_repository import transfer
    from vpn_payment.database.models import Quote

    for n in range(7):
        at = T0 + timedelta(days=n)
        tx = transfer(ui.config, tx_hash=f"0x{n + 1:064x}", timestamp=at)
        await repo.save_transfer(tx)
        await repo.apply_quote(tx.tx_hash, Quote("bitstamp", at, Decimal("2500")))
    sent = await ui.send(callback="history:0")
    assert sent[-1].text.count("+5,00 €") == 5
    assert "1 / 2" in sent[-1].text
    sent = await ui.send(callback="history:1")
    assert sent[-1].text.count("+5,00 €") == 2
    assert "2 / 2" in sent[-1].text
    sent = await ui.send(callback="history:999")
    assert "2 / 2" in sent[-1].text


async def test_repeated_button_click_does_not_send_duplicate_card(ui):
    from aiogram.exceptions import TelegramBadRequest

    original = ui.session.make_request

    async def same_message(bot, method, timeout=None):  # noqa: ASYNC109 - aiogram session interface
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")
        return await original(bot, method, timeout)

    ui.session.make_request = same_message
    await ui.send(callback="balance")
    assert not any(isinstance(x, SendMessage) for x in ui.session.sent)


def test_keyboard_callbacks_fit_telegram_limit():
    for kb in (menu(), history_menu(0, 5), history_menu(2, 5)):
        assert all(len(b.callback_data.encode()) <= 64 for row in kb.inline_keyboard for b in row)


async def test_tariff_has_no_market_label(ui):
    sent = await ui.send("/balance")
    assert "<i>4,00 € / 30 дней</i>" in sent[-1].text
    assert "рыночная оценка" not in sent[-1].text
