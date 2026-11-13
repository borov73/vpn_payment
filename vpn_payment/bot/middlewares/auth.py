from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message


class OwnerOnly(BaseMiddleware):
    def __init__(self, owner_id: int):
        self.owner_id = owner_id

    async def __call__(self, handler, event: Message | CallbackQuery, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if (
            not event.from_user
            or event.from_user.id != self.owner_id
            or not message
            or message.chat.type != "private"
            or message.chat.id != self.owner_id
        ):
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ запрещён", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Доступ запрещён.")
            return None
        return await handler(event, data)
