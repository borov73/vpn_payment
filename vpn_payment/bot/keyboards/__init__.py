from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def menu() -> InlineKeyboardMarkup:
    return keyboard([[("💰 Баланс", "balance"), ("📜 Пополнения", "history:0")]])


def history_menu(page: int, pages: int) -> InlineKeyboardMarkup:
    rows = []
    navigation = []
    if page > 0:
        navigation.append(("← Новее", f"history:{page - 1}"))
    if page + 1 < pages:
        navigation.append(("Раньше →", f"history:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([("💰 Баланс", "balance"), ("📜 Пополнения", "history:0")])
    return keyboard(rows)
