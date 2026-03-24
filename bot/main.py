import html
import io
import json
import logging
import os

import aiohttp
import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_BASE_URL = os.getenv("API_BASE_URL", "").rstrip("/")
BOOKING_WEBAPP_URL = os.getenv("BOOKING_WEBAPP_URL", "").strip()
ROUTES_WEBAPP_URL = os.getenv("ROUTES_WEBAPP_URL", "").strip()
ABOUT_TEXT = os.getenv("ABOUT_TEXT", "Информация о нас скоро появится.")

CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "@username").strip()
RULES_SHORT_TEXT = os.getenv(
    "RULES_SHORT_TEXT",
    "Приезжайте и кайфуйте. Вот такие простые правила у нас.",
).strip()
RULES_PDF_PATH = os.getenv("RULES_PDF_PATH", "").strip()

ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "").strip()
ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not API_BASE_URL:
    raise RuntimeError("API_BASE_URL is not set")
if not BOOKING_WEBAPP_URL:
    raise RuntimeError("BOOKING_WEBAPP_URL is not set")


def parse_admin_chat_id(raw: str) -> int | None:
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None


def parse_admin_user_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except Exception:
            continue
    return out


ADMIN_CHAT_ID = parse_admin_chat_id(ADMIN_CHAT_ID_RAW)
ADMIN_USER_IDS = parse_admin_user_ids(ADMIN_USER_IDS_RAW)

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class BookingFlow(StatesGroup):
    waiting_contact = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def contact_url(username: str) -> str:
    if username.startswith("http://") or username.startswith("https://"):
        return username
    if username.startswith("@"):
        return f"https://t.me/{username[1:]}"
    return f"https://t.me/{username}"


def mention_user(name: str | None, username: str | None, tg_user_id: int | None) -> str:
    safe_name = html.escape(name or "Гость")

    if username:
        safe_username = html.escape(username.lstrip("@"))
        return f'<a href="https://t.me/{safe_username}">{safe_name}</a>'

    if tg_user_id and int(tg_user_id) > 0:
        return f'<a href="tg://user?id={int(tg_user_id)}">{safe_name}</a>'

    return safe_name


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏕️ Забронировать домик", web_app=WebAppInfo(url=BOOKING_WEBAPP_URL))],
            [KeyboardButton(text="📅 Мои брони")],
            [KeyboardButton(text="📞 Связаться с нами")],
            [KeyboardButton(text="❓ Правила проживания")],
        ],
        resize_keyboard=True,
    )


def contact_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def rules_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Открыть PDF", callback_data="rules:pdf")]
        ]
    )


def contact_support_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📞 Написать", url=contact_url(CONTACT_USERNAME))]
        ]
    )


def pay_booking_kb(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил(а)", callback_data=f"paid:{booking_id}")],
            [InlineKeyboardButton(text="❌ Отменить бронь", callback_data=f"cancel:{booking_id}")],
        ]
    )


def cancel_booking_kb(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить бронь", callback_data=f"cancel:{booking_id}")]
        ]
    )


def admin_payment_kb(booking_id: int, guest_tg_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm:confirm:{booking_id}:{guest_tg_user_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm:reject:{booking_id}:{guest_tg_user_id}"),
            ]
        ]
    )


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏳ Ожидают оплаты", callback_data="adm:list:awaiting_payment"),
                InlineKeyboardButton(text="🔄 Ожидают подтверждения", callback_data="adm:list:pending"),
            ],
            [
                InlineKeyboardButton(text="✅ Активные брони", callback_data="adm:list:confirmed"),
                InlineKeyboardButton(text="❌ Отменённые", callback_data="adm:list:cancelled"),
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить панель", callback_data="adm:panel"),
            ],
        ]
    )


def status_label(status: str) -> str:
    mapping = {
        "AWAITING_PAYMENT": "Ожидается оплата",
        "AWAITING_RECEIPT": "Ожидается чек / проверка",
        "PENDING_REVIEW": "Чек получен, ждём подтверждения",
        "CONFIRMED": "Бронь подтверждена",
        "CANCELLED": "Бронь отменена",
        "EXPIRED": "Бронь истекла",
        "COMPLETED": "Бронь завершена",
    }
    return mapping.get(status, status)


def admin_group_title(group: str) -> str:
    mapping = {
        "awaiting_payment": "⏳ Ожидают оплаты",
        "pending": "🔄 Ожидают подтверждения",
        "confirmed": "✅ Активные брони",
        "cancelled": "❌ Отменённые",
    }
    return mapping.get(group, group)


def admin_ui_to_api_group(group: str) -> str:
    mapping = {
        "awaiting_payment": "awaiting_payment",
        "pending": "awaiting_review",
        "confirmed": "confirmed",
        "cancelled": "closed",
    }
    return mapping.get(group, group)


async def api_post(path: str, payload: dict) -> dict:
    url = f"{API_BASE_URL}{path}"
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"API {resp.status}: {text}")
            if "application/json" in (resp.headers.get("Content-Type") or ""):
                return json.loads(text or "{}")
            return {}


def format_booking_item(item: dict) -> str:
    booking_id = item["booking_id"]
    status = status_label(item["status"])
    unit = html.escape(item["unit_title"])
    tariff = html.escape(item["tariff_title"])
    check_in = item["check_in"]
    check_out = item["check_out"]
    adults = item["adults"]
    children = item["children"]
    extra_bed_count = item.get("extra_bed_count", 0)
    total_amount = item["total_amount"]
    prepay_amount = item["prepay_amount"]

    text = (
        f"🏠 <b>{unit}</b>\n"
        f"🧾 Тариф: <b>{tariff}</b>\n"
        f"📅 {check_in} — {check_out}\n"
        f"👨 Взрослые: {adults}\n"
        f"🧒 Дети: {children}\n"
        f"➕ Доп. места: {extra_bed_count}\n"
        f"💰 Итого: <b>{total_amount} ₽</b>\n"
        f"🔻 Предоплата: <b>{prepay_amount} ₽</b>\n"
        f"📌 Статус: <b>{status}</b>\n"
        f"🆔 Бронь: <code>{booking_id}</code>"
    )

    if item.get("expires_at"):
        text += f"\n⏳ Действует до: <code>{item['expires_at']}</code>"

    if not item.get("can_cancel") and item.get("cancel_reason"):
        text += f"\n⚠️ {html.escape(item['cancel_reason'])}"

    return text


def get_admin_guest_id(item: dict) -> int | None:
    value = (
        item.get("guest_tg_user_id")
        or item.get("tg_user_id")
        or item.get("user_tg_user_id")
        or item.get("telegram_user_id")
    )
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except Exception:
        return None


def get_admin_guest_name(item: dict) -> str | None:
    return (
        item.get("guest_name")
        or item.get("telegram_name")
        or item.get("name")
        or None
    )


def get_admin_guest_username(item: dict) -> str | None:
    return (
        item.get("guest_username")
        or item.get("telegram_username")
        or item.get("username")
        or None
    )


def format_admin_booking_item(item: dict) -> str:
    guest_tg_user_id = get_admin_guest_id(item)
    guest_name = get_admin_guest_name(item)
    guest_username = get_admin_guest_username(item)

    guest_line = mention_user(guest_name, guest_username, guest_tg_user_id)

    text = (
        f"🆔 <b>Бронь #{item.get('booking_id')}</b>\n"
        f"👤 Гость: {guest_line}\n"
        f"📱 Телефон: <code>{html.escape(str(item.get('phone', '—')))}</code>\n"
        f"🏠 Вариант: <b>{html.escape(str(item.get('unit_title', '—')))}</b> — "
        f"<b>{html.escape(str(item.get('tariff_title', '—')))}</b>\n"
        f"📅 Даты: <b>{item.get('check_in')} — {item.get('check_out')}</b>\n"
        f"👨 Взрослые: {item.get('adults', 0)}\n"
        f"🧒 Дети: {item.get('children', 0)}\n"
        f"➕ Доп. места: {item.get('extra_bed_count', 0)}\n"
        f"💰 Итого: <b>{item.get('total_amount', 0)} ₽</b>\n"
        f"🔻 Предоплата: <b>{item.get('prepay_amount', 0)} ₽</b>\n"
        f"📌 Статус: <b>{status_label(str(item.get('status', '—')))}</b>\n"
        f"📎 Чек: <b>{'да' if item.get('receipt_attached') else 'нет'}</b>\n"
        f"🕒 Создано: <code>{item.get('created_at', '—')}</code>"
    )

    if item.get("expires_at"):
        text += f"\n⏳ Оплатить до: <code>{item['expires_at']}</code>"

    return text


def build_bank_qr_payload(*, recipient_name: str, bank_name: str, personal_acc: str, bic: str, corr_acc: str, purpose: str, sum_rub: int) -> str:
    kopeks = int(sum_rub) * 100
    parts = [
        "ST00012",
        f"Name={recipient_name}",
        f"PersonalAcc={personal_acc}",
        f"BankName={bank_name}",
        f"BIC={bic}",
        f"CorrespAcc={corr_acc}",
        f"Purpose={purpose}",
        f"Sum={kopeks}",
    ]
    return "|".join(parts)


def build_qr_png_bytes(payload: str) -> bytes:
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def payment_text(created: dict, booking_data: dict) -> str:
    profile = created["payment_profile"]
    booking_id = created["booking_id"]

    return (
        "🏕️ <b>Бронь создана</b>\n\n"
        f"🆔 Номер брони: <code>{booking_id}</code>\n"
        f"🏠 Вариант: <b>{html.escape(booking_data['unit_title'])}</b> — <b>{html.escape(booking_data['tariff_title'])}</b>\n"
        f"📅 Даты: <b>{booking_data['check_in']} — {booking_data['check_out']}</b>\n"
        f"👨 Взрослые: {booking_data['adults']}\n"
        f"🧒 Дети: {booking_data['children']}\n"
        f"➕ Доп. места: {booking_data['extra_bed_count']}\n\n"
        f"💰 Итого: <b>{booking_data['total_amount']} ₽</b>\n"
        f"🔻 К оплате сейчас: <b>{booking_data['prepay_amount']} ₽</b>\n\n"
        "💳 <b>Реквизиты для оплаты</b>\n"
        f"Получатель: <b>{html.escape(profile['recipient_name'])}</b>\n"
        f"Банк: <b>{html.escape(profile['bank_name'])}</b>\n"
        f"Счёт: <code>{html.escape(profile['personal_acc'])}</code>\n"
        f"БИК: <code>{html.escape(profile['bic'])}</code>\n"
        f"Корр. счёт: <code>{html.escape(profile['corr_acc'])}</code>\n\n"
        f"⏳ Оплатить до: <code>{created['expires_at']}</code>\n\n"
        "⚠️ После оплаты обязательно нажмите кнопку ниже."
    )


def build_payment_qr(created: dict, booking_data: dict) -> bytes | None:
    profile = created["payment_profile"]
    booking_id = created["booking_id"]

    if not all([
        profile.get("recipient_name"),
        profile.get("bank_name"),
        profile.get("personal_acc"),
        profile.get("bic"),
        profile.get("corr_acc"),
    ]):
        return None

    purpose = f"Бронь #{booking_id} Тишь да гладь"
    qr_payload = build_bank_qr_payload(
        recipient_name=profile["recipient_name"],
        bank_name=profile["bank_name"],
        personal_acc=profile["personal_acc"],
        bic=profile["bic"],
        corr_acc=profile["corr_acc"],
        purpose=purpose,
        sum_rub=int(booking_data["prepay_amount"]),
    )
    return build_qr_png_bytes(qr_payload)


async def get_booking_item_for_user(tg_user_id: int, booking_id: int) -> dict | None:
    data = await api_post("/api/v2/bookings/active", {"tg_user_id": tg_user_id})
    items = data.get("items", [])
    for item in items:
        if int(item["booking_id"]) == int(booking_id):
            return item
    return None


async def notify_admin_new_booking(*, guest_tg_user_id: int, guest_name: str | None, guest_username: str | None, phone: str, created: dict, booking_data: dict):
    if not ADMIN_CHAT_ID:
        return

    user_ref = mention_user(guest_name, guest_username, guest_tg_user_id)

    text = (
        "📝 <b>Новая бронь создана</b>\n\n"
        f"🆔 Бронь: <code>{created['booking_id']}</code>\n"
        f"👤 Гость: {user_ref}\n"
        f"📱 Телефон: <code>{html.escape(phone)}</code>\n"
        f"🏠 Вариант: <b>{html.escape(booking_data['unit_title'])}</b> — <b>{html.escape(booking_data['tariff_title'])}</b>\n"
        f"📅 Даты: <b>{booking_data['check_in']} — {booking_data['check_out']}</b>\n"
        f"👨 Взрослые: {booking_data['adults']}\n"
        f"🧒 Дети: {booking_data['children']}\n"
        f"➕ Доп. места: {booking_data['extra_bed_count']}\n"
        f"💰 Итого: <b>{booking_data['total_amount']} ₽</b>\n"
        f"🔻 Предоплата: <b>{booking_data['prepay_amount']} ₽</b>\n"
        f"📌 Статус: <b>{status_label(created['status'])}</b>"
    )

    try:
        await bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML")
    except Exception:
        logging.exception("Failed to notify admin about new booking")


async def notify_admin_payment_marked(*, guest_tg_user_id: int, guest_name: str | None, guest_username: str | None, phone: str, item: dict):
    if not ADMIN_CHAT_ID:
        return

    user_ref = mention_user(guest_name, guest_username, guest_tg_user_id)

    text = (
        "💳 <b>Гость отметил оплату</b>\n\n"
        f"🆔 Бронь: <code>{item['booking_id']}</code>\n"
        f"👤 Гость: {user_ref}\n"
        f"📱 Телефон: <code>{html.escape(phone)}</code>\n"
        f"🏠 Вариант: <b>{html.escape(item['unit_title'])}</b> — <b>{html.escape(item['tariff_title'])}</b>\n"
        f"📅 Даты: <b>{item['check_in']} — {item['check_out']}</b>\n"
        f"👨 Взрослые: {item['adults']}\n"
        f"🧒 Дети: {item['children']}\n"
        f"➕ Доп. места: {item.get('extra_bed_count', 0)}\n"
        f"💰 Итого: <b>{item['total_amount']} ₽</b>\n"
        f"🔻 Предоплата: <b>{item['prepay_amount']} ₽</b>\n"
        f"📌 Статус: <b>{status_label(item['status'])}</b>\n\n"
        "Чек: <b>не прикреплён</b>"
    )

    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            text,
            parse_mode="HTML",
            reply_markup=admin_payment_kb(item["booking_id"], guest_tg_user_id),
        )
    except Exception:
        logging.exception("Failed to notify admin about payment mark")


async def notify_admin_receipt(*, guest_tg_user_id: int, guest_name: str | None, guest_username: str | None, phone: str, item: dict, file_type: str, file_id: str):
    if not ADMIN_CHAT_ID:
        return

    user_ref = mention_user(guest_name, guest_username, guest_tg_user_id)

    caption = (
        "📎 <b>Гость прикрепил чек</b>\n\n"
        f"🆔 Бронь: <code>{item['booking_id']}</code>\n"
        f"👤 Гость: {user_ref}\n"
        f"📱 Телефон: <code>{html.escape(phone)}</code>\n"
        f"🏠 Вариант: <b>{html.escape(item['unit_title'])}</b> — <b>{html.escape(item['tariff_title'])}</b>\n"
        f"📅 Даты: <b>{item['check_in']} — {item['check_out']}</b>\n"
        f"👨 Взрослые: {item['adults']}\n"
        f"🧒 Дети: {item['children']}\n"
        f"➕ Доп. места: {item.get('extra_bed_count', 0)}\n"
        f"💰 Итого: <b>{item['total_amount']} ₽</b>\n"
        f"🔻 Предоплата: <b>{item['prepay_amount']} ₽</b>\n"
        f"📌 Статус: <b>{status_label(item['status'])}</b>\n\n"
        "Чек: <b>прикреплён</b>"
    )

    try:
        if file_type == "photo":
            await bot.send_photo(
                ADMIN_CHAT_ID,
                photo=file_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=admin_payment_kb(item["booking_id"], guest_tg_user_id),
            )
        else:
            await bot.send_document(
                ADMIN_CHAT_ID,
                document=file_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=admin_payment_kb(item["booking_id"], guest_tg_user_id),
            )
    except Exception:
        logging.exception("Failed to notify admin about receipt")


async def notify_admin_cancel(*, guest_tg_user_id: int, booking_id: int):
    if not ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"❌ <b>Бронь отменена гостем</b>\n\n🆔 Бронь: <code>{booking_id}</code>\n👤 tg_user_id: <code>{guest_tg_user_id}</code>",
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Failed to notify admin about cancellation")


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏕️ <b>Добро пожаловать в «Тишь да Гладь»!</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.", reply_markup=main_kb())
        return

    text = (
        "🛠 <b>Админ-панель</b>\n\n"
        "Выберите раздел ниже:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "adm:panel")
async def admin_panel_refresh(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback.answer("Обновлено")
    try:
        await callback.message.edit_text(
            "🛠 <b>Админ-панель</b>\n\nВыберите раздел ниже:",
            parse_mode="HTML",
            reply_markup=admin_panel_kb(),
        )
    except Exception:
        pass


@dp.callback_query(F.data.startswith("adm:list:"))
async def admin_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    ui_group = callback.data.split(":")[2]
    api_group = admin_ui_to_api_group(ui_group)

    try:
        data = await api_post(
            "/api/v2/admin/bookings/list",
            {
                "status_group": api_group,
                "limit": 10,
            },
        )
    except Exception as e:
        await callback.answer("Ошибка загрузки", show_alert=True)
        await callback.message.answer(f"Ошибка:\n{e}")
        return

    await callback.answer()

    title = admin_group_title(ui_group)
    items = data.get("items", [])
    count = data.get("count", len(items))

    try:
        await callback.message.edit_text(
            f"🛠 <b>Админ-панель</b>\n\nРаздел: <b>{html.escape(title)}</b>\nНайдено: <b>{count}</b>",
            parse_mode="HTML",
            reply_markup=admin_panel_kb(),
        )
    except Exception:
        pass

    if not items:
        await callback.message.answer(
            f"В разделе «{title}» пока пусто.",
            reply_markup=main_kb(),
        )
        return

    for item in items:
        guest_tg_user_id = get_admin_guest_id(item)
        reply_markup = None

        if item.get("status") in ("AWAITING_RECEIPT", "PENDING_REVIEW") and guest_tg_user_id:
            reply_markup = admin_payment_kb(item["booking_id"], guest_tg_user_id)

        await callback.message.answer(
            format_admin_booking_item(item),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )


@dp.message(F.text == "🏕️ Забронировать домик")
async def booking_fallback(message: Message):
    await message.answer(
        "Откройте сценарий бронирования через кнопку «🏕️ Забронировать домик».",
        reply_markup=main_kb(),
    )


@dp.message(F.text == "📅 Мои брони")
async def show_active_bookings(message: Message):
    try:
        data = await api_post(
            "/api/v2/bookings/active",
            {"tg_user_id": message.from_user.id},
        )
    except Exception as e:
        await message.answer(
            f"Не удалось получить брони:\n{e}",
            reply_markup=main_kb(),
        )
        return

    items = data.get("items", [])
    if not items:
        await message.answer(
            "У вас пока нет активных броней.",
            reply_markup=main_kb(),
        )
        return

    await message.answer(
        f"📅 Найдено броней: <b>{len(items)}</b>",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )

    for item in items:
        if item["status"] == "AWAITING_PAYMENT":
            reply_markup = pay_booking_kb(item["booking_id"])
        elif item.get("can_cancel"):
            reply_markup = cancel_booking_kb(item["booking_id"])
        else:
            reply_markup = None

        await message.answer(
            format_booking_item(item),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )


@dp.message(F.text == "📞 Связаться с нами")
async def contact_us(message: Message):
    await message.answer(
        f"📞 Связаться с нами можно здесь: <b>{html.escape(CONTACT_USERNAME)}</b>",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )
    await message.answer(
        "Нажмите кнопку ниже:",
        reply_markup=contact_support_kb(),
    )


@dp.message(F.text == "❓ Правила проживания")
async def rules(message: Message):
    await message.answer(
        f"❓ <b>Правила проживания</b>\n\n{html.escape(RULES_SHORT_TEXT)}",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )
    await message.answer(
        "Открыть полный PDF:",
        reply_markup=rules_kb(),
    )


@dp.callback_query(F.data == "rules:pdf")
async def rules_pdf(callback: CallbackQuery):
    await callback.answer()

    if not RULES_PDF_PATH or not os.path.exists(RULES_PDF_PATH):
        await callback.message.answer(
            "PDF с правилами пока недоступен.",
            reply_markup=main_kb(),
        )
        return

    await callback.message.answer_document(
        FSInputFile(RULES_PDF_PATH),
        caption="📄 Полная версия правил проживания",
        reply_markup=main_kb(),
    )


@dp.message(F.web_app_data)
async def on_webapp_data(message: Message, state: FSMContext):
    raw = (message.web_app_data.data or "").strip()

    try:
        payload = json.loads(raw)
    except Exception:
        await message.answer("Не удалось прочитать данные из WebApp.", reply_markup=main_kb())
        return

    if payload.get("type") != "booking_calculated":
        await message.answer("Получены неизвестные данные из WebApp.", reply_markup=main_kb())
        return

    data = payload.get("data") or {}
    required_keys = [
        "unit_id", "tariff_id", "adults", "children",
        "extra_bed_count", "check_in", "check_out",
        "total_amount", "prepay_amount", "unit_title", "tariff_title",
    ]
    if not all(k in data for k in required_keys):
        await message.answer("Из WebApp пришли неполные данные.", reply_markup=main_kb())
        return

    await state.update_data(webapp_booking=data)
    await state.set_state(BookingFlow.waiting_contact)

    summary = (
        "✅ <b>Выбор сохранён</b>\n\n"
        f"🏠 {html.escape(data['unit_title'])} — {html.escape(data['tariff_title'])}\n"
        f"📅 {data['check_in']} — {data['check_out']}\n"
        f"👨 Взрослые: {data['adults']}\n"
        f"🧒 Дети: {data['children']}\n"
        f"➕ Доп. места: {data['extra_bed_count']}\n"
        f"💰 Итого: <b>{data['total_amount']} ₽</b>\n"
        f"🔻 Предоплата: <b>{data['prepay_amount']} ₽</b>\n\n"
        "Теперь отправьте номер телефона кнопкой ниже."
    )

    await message.answer(
        summary,
        parse_mode="HTML",
        reply_markup=contact_kb(),
    )


@dp.message(BookingFlow.waiting_contact, F.text == "❌ Отмена")
async def cancel_contact_step(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, оформление отменено.", reply_markup=main_kb())


@dp.message(BookingFlow.waiting_contact, F.contact)
async def on_contact(message: Message, state: FSMContext):
    if message.contact.user_id and message.contact.user_id != message.from_user.id:
        await message.answer(
            "Пожалуйста, отправьте именно свой номер телефона.",
            reply_markup=contact_kb(),
        )
        return

    state_data = await state.get_data()
    booking_data = state_data.get("webapp_booking")

    if not booking_data:
        await state.clear()
        await message.answer(
            "Данные бронирования не найдены. Начните заново через кнопку «🏕️ Забронировать домик».",
            reply_markup=main_kb(),
        )
        return

    payload = {
        "tg_user_id": message.from_user.id,
        "phone": message.contact.phone_number,
        "telegram_name": message.from_user.full_name,
        "telegram_username": message.from_user.username,
        "unit_id": booking_data["unit_id"],
        "tariff_id": booking_data["tariff_id"],
        "adults": booking_data["adults"],
        "children": booking_data["children"],
        "extra_bed_count": booking_data["extra_bed_count"],
        "check_in": booking_data["check_in"],
        "check_out": booking_data["check_out"],
    }

    try:
        created = await api_post("/api/v2/bookings/create", payload)
    except Exception as e:
        await state.clear()
        await message.answer(
            f"Не удалось создать бронь:\n{e}",
            reply_markup=main_kb(),
        )
        return

    await state.clear()

    await notify_admin_new_booking(
        guest_tg_user_id=message.from_user.id,
        guest_name=message.from_user.full_name,
        guest_username=message.from_user.username,
        phone=message.contact.phone_number,
        created=created,
        booking_data=booking_data,
    )

    text = payment_text(created, booking_data)
    qr_bytes = build_payment_qr(created, booking_data)

    await message.answer(
        "🏕️ Бронь создана. Ниже реквизиты для оплаты.",
        reply_markup=ReplyKeyboardRemove(),
    )

    if qr_bytes:
        qr_input = BufferedInputFile(qr_bytes, filename="payment_qr.png")
        await message.answer_photo(
            photo=qr_input,
            caption=text,
            parse_mode="HTML",
            reply_markup=pay_booking_kb(created["booking_id"]),
        )
    else:
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=pay_booking_kb(created["booking_id"]),
        )


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_booking(callback: CallbackQuery):
    booking_id = int(callback.data.split(":")[1])

    try:
        result = await api_post(
            "/api/v2/bookings/cancel",
            {
                "tg_user_id": callback.from_user.id,
                "booking_id": booking_id,
            },
        )
    except Exception as e:
        await callback.answer("Не удалось отменить бронь", show_alert=True)
        await callback.message.answer(f"Ошибка отмены:\n{e}")
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await notify_admin_cancel(
        guest_tg_user_id=callback.from_user.id,
        booking_id=booking_id,
    )

    await callback.answer("Бронь отменена")
    await callback.message.answer(
        f"✅ Бронь <code>{result['booking_id']}</code> отменена.",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


@dp.callback_query(F.data.startswith("paid:"))
async def paid_click(callback: CallbackQuery):
    booking_id = int(callback.data.split(":")[1])

    try:
        result = await api_post(
            "/api/v2/bookings/paid-click",
            {
                "tg_user_id": callback.from_user.id,
                "booking_id": booking_id,
            },
        )
    except Exception as e:
        await callback.answer("Не удалось отметить оплату", show_alert=True)
        await callback.message.answer(f"Ошибка:\n{e}")
        return

    item = await get_booking_item_for_user(callback.from_user.id, booking_id)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if item:
        await notify_admin_payment_marked(
            guest_tg_user_id=callback.from_user.id,
            guest_name=callback.from_user.full_name,
            guest_username=callback.from_user.username,
            phone="не указан в уведомлении",
            item=item,
        )

    await callback.answer("Отмечено")
    await callback.message.answer(
        "✅ Спасибо!\n\n"
        "Мы получили отметку об оплате.\n"
        "Если у вас есть чек или скрин перевода — отправьте его следующим сообщением. Это ускорит проверку.\n\n"
        f"Статус: <b>{status_label(result['status'])}</b>",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


async def find_receipt_target(tg_user_id: int, preferred_booking_id: int | None = None) -> dict | None:
    data = await api_post("/api/v2/bookings/active", {"tg_user_id": tg_user_id})
    items = data.get("items", [])

    candidates = [
        item for item in items
        if item["status"] in ("AWAITING_RECEIPT", "PENDING_REVIEW")
    ]

    if preferred_booking_id is not None:
        for item in candidates:
            if int(item["booking_id"]) == int(preferred_booking_id):
                return item
        return None

    if len(candidates) == 1:
        return candidates[0]

    return None


@dp.message(F.photo)
async def on_receipt_photo(message: Message):
    try:
        target = await find_receipt_target(message.from_user.id)
    except Exception as e:
        await message.answer(f"Не удалось проверить брони:\n{e}", reply_markup=main_kb())
        return

    if not target:
        return

    try:
        result = await api_post(
            "/api/v2/bookings/receipt-uploaded",
            {
                "tg_user_id": message.from_user.id,
                "booking_id": target["booking_id"],
            },
        )
    except Exception as e:
        await message.answer(f"Не удалось прикрепить чек:\n{e}", reply_markup=main_kb())
        return

    await notify_admin_receipt(
        guest_tg_user_id=message.from_user.id,
        guest_name=message.from_user.full_name,
        guest_username=message.from_user.username,
        phone="не указан в уведомлении",
        item={**target, "status": result["status"]},
        file_type="photo",
        file_id=message.photo[-1].file_id,
    )

    await message.answer(
        "📎 Чек получен.\n"
        f"Статус: <b>{status_label(result['status'])}</b>\n"
        "Ожидаем подтверждение администратора.",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


@dp.message(F.document)
async def on_receipt_document(message: Message):
    try:
        target = await find_receipt_target(message.from_user.id)
    except Exception as e:
        await message.answer(f"Не удалось проверить брони:\n{e}", reply_markup=main_kb())
        return

    if not target:
        return

    try:
        result = await api_post(
            "/api/v2/bookings/receipt-uploaded",
            {
                "tg_user_id": message.from_user.id,
                "booking_id": target["booking_id"],
            },
        )
    except Exception as e:
        await message.answer(f"Не удалось прикрепить чек:\n{e}", reply_markup=main_kb())
        return

    await notify_admin_receipt(
        guest_tg_user_id=message.from_user.id,
        guest_name=message.from_user.full_name,
        guest_username=message.from_user.username,
        phone="не указан в уведомлении",
        item={**target, "status": result["status"]},
        file_type="document",
        file_id=message.document.file_id,
    )

    await message.answer(
        "📎 Чек получен.\n"
        f"Статус: <b>{status_label(result['status'])}</b>\n"
        "Ожидаем подтверждение администратора.",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


@dp.callback_query(F.data.startswith("adm:confirm:") | F.data.startswith("adm:reject:"))
async def admin_action(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    action = parts[1]
    booking_id = int(parts[2])
    guest_tg_user_id = int(parts[3])

    if action == "confirm":
        try:
            await api_post(
                "/api/v2/admin/bookings/confirm",
                {
                    "booking_id": booking_id,
                    "admin_tg_user_id": callback.from_user.id,
                },
            )
        except Exception as e:
            await callback.answer("Ошибка подтверждения", show_alert=True)
            await callback.message.answer(f"Ошибка:\n{e}")
            return

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.answer("Подтверждено")
        await callback.message.answer(
            f"✅ Бронь <code>{booking_id}</code> подтверждена.",
            parse_mode="HTML",
        )

        try:
            await bot.send_message(
                guest_tg_user_id,
                "🏕️ <b>Бронирование подтверждено!</b>\n\n"
                f"🆔 Номер брони: <code>{booking_id}</code>\n"
                "✅ Оплата подтверждена администратором.\n"
                "Ждём вас в «Тишь да Гладь»! 🌲🔥",
                parse_mode="HTML",
                reply_markup=main_kb(),
            )
        except Exception:
            logging.exception("Failed to notify guest about confirmed booking")

        return

    if action == "reject":
        try:
            await api_post(
                "/api/v2/admin/bookings/reject",
                {
                    "booking_id": booking_id,
                    "admin_tg_user_id": callback.from_user.id,
                },
            )
        except Exception as e:
            await callback.answer("Ошибка отклонения", show_alert=True)
            await callback.message.answer(f"Ошибка:\n{e}")
            return

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.answer("Отклонено")
        await callback.message.answer(
            f"❌ Бронь <code>{booking_id}</code> отклонена.",
            parse_mode="HTML",
        )

        try:
            await bot.send_message(
                guest_tg_user_id,
                "❌ <b>Мы не смогли подтвердить оплату.</b>\n\n"
                f"🆔 Номер брони: <code>{booking_id}</code>\n"
                f"Свяжитесь с нами: {html.escape(CONTACT_USERNAME)}",
                parse_mode="HTML",
                reply_markup=main_kb(),
            )
        except Exception:
            logging.exception("Failed to notify guest about rejected booking")

        return

    await callback.answer("Неизвестное действие", show_alert=True)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
