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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
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
ADMIN_WEBAPP_URL = os.getenv("ADMIN_WEBAPP_URL", "").strip()

CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "@username").strip()
RULES_SHORT_TEXT = """ДОРОГИЕ ГОСТИ!
Просим заранее ознакомиться с правилами проживания 😊

Время заезда и выезда отличаются в зависимости от дома. В момент бронирования время будет указано.

Наш дом – это место семейного тихого отдыха! Мы построили его, вкладывая свою душу.
Так давайте отдыхать так, чтобы ваш отдых оставил только приятные впечатления для всех.

Количество взрослых гостей мы ограничиваем до 2-х, с детьми вместимость до 4-5 (в зависимости от возраста).

НА ТЕРРИТОРИИ ДЕЙСТВУЕТ "ЗАКОН ТИШИНЫ"

Шуметь после 21:00 на улице ЗАПРЕЩЕНО.
Музыку ГРОМКО не включать.
Музыкальные колонки также запрещены, в доме есть Яндекс станция и телевизор.

Если вы планируете вечеринку с распитием алкогольных напитков, то мы вам точно не подходим
и рекомендуем подобрать другое место.

ФЕЙЕРВЕРКИ И ХЛОПУШКИ НА ТЕРРИТОРИИ И ЗА ЕЕ ПРЕДЕЛАМИ ❌ ЗАПРЕЩЕНЫ ❌

МЫ НЕ ЗАСЕЛЯЕМ С ПИТОМЦАМИ, ПРОСЬБА ОТНЕСТИСЬ С ПОНИМАНИЕМ.

ПРИ ВНЕСЕНИИ ПРЕДОПЛАТЫ вы автоматически соглашаетесь с правилами изложенными выше
и НЕСЁТЕ ОТВЕТСТВЕННОСТЬ за их соблюдение!""".strip()
RULES_PDF_PATH = os.getenv("RULES_PDF_PATH", "").strip()

ROUTE_PLACE_NAME = os.getenv("ROUTE_PLACE_NAME", "Тишь да Гладь").strip()
ROUTE_ADDRESS_TEXT = os.getenv("ROUTE_ADDRESS_TEXT", "Адрес скоро добавим.").strip()
ROUTE_HINT_TEXT = os.getenv(
    "ROUTE_HINT_TEXT",
    "Если будет сложно найти — напишите нам, мы подскажем.",
).strip()

ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "").strip()
ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "").strip()

RULES_VERSION = "2026-03-30"

START_TEXT = (
    "🏕️ <b>Добро пожаловать в «Тишь да Гладь»!</b>\n\n"
    "Здесь слышно тишину.\n"
    "Не метафора — реальное ощущение, за которым к нам приезжают. "
    "Когда вокруг нет соседей, нет лишних людей и город остаётся далеко позади.\n\n"
    "Давайте знакомиться, меня зовут Наталья, я идейный вдохновитель и заботливая хозяйка "
    "уникального пространства «Тишь да Гладь».\n"
    "В период пандемии мы с супругом решили заняться строительством дома, вдали от городской "
    "суеты, среди живописных лесов и полей.\n\n"
    "«Тишь да Гладь» — место силы.\n"
    "Небольшой уголок для душевного отдыха, где можно остановиться, выдохнуть и побыть в тишине.\n"
    "Здесь вас ждут 2 современных уютных домика, а рядом — жаркая баня и горячий чан, "
    "которые особенно хороши вечером, когда вокруг становится совсем тихо.\n\n"
    "Нажмите <b>«Забронировать домик»</b>, чтобы посмотреть даты и цены."
)

BOOKING_GATE_TEXT = (
    "❗ <b>Перед бронированием нужно ознакомиться с правилами проживания.</b>\n\n"
    "Пожалуйста, прочитайте правила и подтвердите, что вы ознакомились с ними. "
    "После этого я открою бронирование."
)

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

ADMIN_MESSAGES_PATH = os.path.join(os.path.dirname(__file__), "admin_messages.json")
BOOKING_CONTACTS_PATH = os.path.join(os.path.dirname(__file__), "booking_contacts.json")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Временное хранение подтверждения правил.
# После рестарта бота пользователь подтвердит их заново.
accepted_rules_by_user: dict[int, str] = {}


class BookingFlow(StatesGroup):
    waiting_contact = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def has_accepted_rules(user_id: int) -> bool:
    return accepted_rules_by_user.get(user_id) == RULES_VERSION


def mark_rules_accepted(user_id: int) -> None:
    accepted_rules_by_user[user_id] = RULES_VERSION


def no_preview() -> LinkPreviewOptions:
    return LinkPreviewOptions(is_disabled=True)


def contact_url(username: str) -> str:
    if username.startswith("http://") or username.startswith("https://"):
        return username
    if username.startswith("@"):
        return f"https://t.me/{username[1:]}"
    return f"https://t.me/{username}"


def mention_user(name: str | None, username: str | None, tg_user_id: int | None) -> str:
    if username:
        safe_username = html.escape(username.lstrip("@"))
        return f'<a href="https://t.me/{safe_username}">@{safe_username}</a>'

    safe_name = html.escape(name or "Гость")

    if tg_user_id and int(tg_user_id) > 0:
        return f'<a href="tg://user?id={int(tg_user_id)}">{safe_name}</a>'

    return safe_name


def build_how_to_get_text() -> str:
    safe_place = html.escape(ROUTE_PLACE_NAME)
    safe_address = html.escape(ROUTE_ADDRESS_TEXT)
    safe_hint = html.escape(ROUTE_HINT_TEXT)

    return (
        f"📍 <b>Как добраться</b>\n\n"
        f"<b>{safe_place}</b>\n"
        f"Адрес: <code>{safe_address}</code>\n\n"
        f"{safe_hint}"
    )


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏕️ Забронировать домик")],
            [KeyboardButton(text="📅 Мои брони")],
            [KeyboardButton(text="📞 Связаться с нами")],
            [KeyboardButton(text="📍 Как добраться")],
            [KeyboardButton(text="❓ Правила проживания")],
        ],
        resize_keyboard=True,
    )


def booking_open_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏕️ Открыть бронирование", web_app=WebAppInfo(url=BOOKING_WEBAPP_URL))]
        ]
    )


def admin_webapp_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Открыть админку", web_app=WebAppInfo(url=ADMIN_WEBAPP_URL))]
        ]
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


def rules_gate_kb() -> InlineKeyboardMarkup:
    rows = []

    if RULES_PDF_PATH and os.path.exists(RULES_PDF_PATH):
        rows.append([InlineKeyboardButton(text="📄 Открыть PDF", callback_data="rules:pdf")])

    rows.append([InlineKeyboardButton(text="✅ С правилами ознакомлен(а)", callback_data="rules:accept")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="rules:back")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def contact_support_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📞 Написать", url=contact_url(CONTACT_USERNAME))]
        ]
    )


def route_kb() -> InlineKeyboardMarkup | None:
    if not ROUTES_WEBAPP_URL:
        return None

    rows = [
        [InlineKeyboardButton(text="📍 Открыть в Яндекс Картах", url=ROUTES_WEBAPP_URL)],
        [InlineKeyboardButton(text="📞 Связаться с нами", url=contact_url(CONTACT_USERNAME))],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def load_admin_messages() -> dict:
    try:
        if not os.path.exists(ADMIN_MESSAGES_PATH):
            return {}
        with open(ADMIN_MESSAGES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logging.exception("Failed to load admin messages map")
        return {}


def save_admin_messages(data: dict) -> None:
    try:
        tmp_path = ADMIN_MESSAGES_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, ADMIN_MESSAGES_PATH)
    except Exception:
        logging.exception("Failed to save admin messages map")


def remember_admin_message(booking_id: int, *, chat_id: int, message_id: int, kind: str) -> None:
    data = load_admin_messages()
    data[str(booking_id)] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "kind": kind,
    }
    save_admin_messages(data)


def get_admin_message_ref(booking_id: int) -> dict | None:
    data = load_admin_messages()
    return data.get(str(booking_id))


def forget_admin_message(booking_id: int) -> None:
    data = load_admin_messages()
    if str(booking_id) in data:
        data.pop(str(booking_id), None)
        save_admin_messages(data)


def load_booking_contacts() -> dict:
    try:
        if not os.path.exists(BOOKING_CONTACTS_PATH):
            return {}
        with open(BOOKING_CONTACTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logging.exception("Failed to load booking contacts map")
        return {}


def save_booking_contacts(data: dict) -> None:
    try:
        tmp_path = BOOKING_CONTACTS_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, BOOKING_CONTACTS_PATH)
    except Exception:
        logging.exception("Failed to save booking contacts map")


def remember_booking_contact(
    booking_id: int,
    *,
    tg_user_id: int | None,
    guest_name: str | None,
    guest_username: str | None,
    phone: str | None,
) -> None:
    data = load_booking_contacts()
    data[str(booking_id)] = {
        "tg_user_id": tg_user_id,
        "guest_name": guest_name,
        "guest_username": guest_username,
        "phone": phone,
    }
    save_booking_contacts(data)


def get_booking_contact(booking_id: int) -> dict | None:
    data = load_booking_contacts()
    return data.get(str(booking_id))


def forget_booking_contact(booking_id: int) -> None:
    data = load_booking_contacts()
    if str(booking_id) in data:
        data.pop(str(booking_id), None)
        save_booking_contacts(data)


def replace_status_line(text: str, new_status: str) -> str:
    if not text:
        return f"📌 Статус: <b>{new_status}</b>"

    lines = text.splitlines()
    replaced = False

    for i, line in enumerate(lines):
        if line.startswith("📌 Статус:"):
            lines[i] = f"📌 Статус: <b>{new_status}</b>"
            replaced = True
            break

    if not replaced:
        lines.append(f"📌 Статус: <b>{new_status}</b>")

    return "\n".join(lines)


async def send_or_replace_admin_booking_message(
    *,
    booking_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    file_type: str | None = None,
    file_id: str | None = None,
) -> None:
    if not ADMIN_CHAT_ID:
        return

    old_ref = get_admin_message_ref(booking_id)
    if old_ref:
        try:
            await bot.delete_message(
                chat_id=old_ref["chat_id"],
                message_id=old_ref["message_id"],
            )
        except Exception:
            logging.exception("Failed to delete previous admin booking message")

    try:
        if file_type == "photo" and file_id:
            msg = await bot.send_photo(
                ADMIN_CHAT_ID,
                photo=file_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            remember_admin_message(
                booking_id,
                chat_id=ADMIN_CHAT_ID,
                message_id=msg.message_id,
                kind="photo",
            )
            return

        if file_type == "document" and file_id:
            msg = await bot.send_document(
                ADMIN_CHAT_ID,
                document=file_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            remember_admin_message(
                booking_id,
                chat_id=ADMIN_CHAT_ID,
                message_id=msg.message_id,
                kind="document",
            )
            return

        msg = await bot.send_message(
            ADMIN_CHAT_ID,
            text,
            parse_mode="HTML",
            link_preview_options=no_preview(),
            reply_markup=reply_markup,
        )
        remember_admin_message(
            booking_id,
            chat_id=ADMIN_CHAT_ID,
            message_id=msg.message_id,
            kind="text",
        )
    except Exception:
        logging.exception("Failed to send or replace admin booking message")


async def edit_admin_booking_message(
    *,
    booking_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    ref = get_admin_message_ref(booking_id)
    if not ref:
        return False

    try:
        kind = ref.get("kind")
        if kind == "text":
            await bot.edit_message_text(
                text=text,
                chat_id=ref["chat_id"],
                message_id=ref["message_id"],
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        else:
            await bot.edit_message_caption(
                caption=text,
                chat_id=ref["chat_id"],
                message_id=ref["message_id"],
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        return True
    except Exception:
        logging.exception("Failed to edit admin booking message")
        return False


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


async def notify_admin_new_booking(
    *,
    guest_tg_user_id: int,
    guest_name: str | None,
    guest_username: str | None,
    phone: str,
    created: dict,
    booking_data: dict,
):
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
        await send_or_replace_admin_booking_message(
            booking_id=created["booking_id"],
            text=text,
        )
    except Exception:
        logging.exception("Failed to notify admin about new booking")


async def notify_admin_receipt(
    *,
    guest_tg_user_id: int,
    guest_name: str | None,
    guest_username: str | None,
    phone: str,
    item: dict,
    file_type: str,
    file_id: str,
):
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
        await send_or_replace_admin_booking_message(
            booking_id=item["booking_id"],
            text=caption,
            reply_markup=admin_payment_kb(item["booking_id"], guest_tg_user_id),
            file_type=file_type,
            file_id=file_id,
        )
    except Exception:
        logging.exception("Failed to notify admin about receipt")


async def notify_admin_cancel(
    *,
    guest_tg_user_id: int | None,
    guest_name: str | None,
    guest_username: str | None,
    booking_id: int,
):
    if not ADMIN_CHAT_ID:
        return

    guest_line = mention_user(guest_name, guest_username, guest_tg_user_id)

    text = (
        "❌ <b>Бронь отменена гостем</b>\n\n"
        f"🆔 Бронь: <code>{booking_id}</code>\n"
        f"👤 Гость: {guest_line}\n"
        "📌 Статус: <b>Бронь отменена</b>"
    )

    updated = await edit_admin_booking_message(
        booking_id=booking_id,
        text=text,
        reply_markup=None,
    )

    if not updated:
        try:
            await send_or_replace_admin_booking_message(
                booking_id=booking_id,
                text=text,
            )
        except Exception:
            logging.exception("Failed to notify admin about cancellation")


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        START_TEXT,
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


@dp.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.", reply_markup=main_kb())
        return

    await state.clear()

    if not ADMIN_WEBAPP_URL:
        await message.answer("ADMIN_WEBAPP_URL не задан.", reply_markup=main_kb())
        return

    text = (
        "🛠 <b>Админка бронирований</b>\n\n"
        "Нажми кнопку ниже, чтобы открыть панель управления текущими бронями."
    )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=admin_webapp_kb(),
    )


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
async def booking_entry(message: Message, state: FSMContext):
    await state.clear()

    if not has_accepted_rules(message.from_user.id):
        await message.answer(
            BOOKING_GATE_TEXT,
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
        await message.answer(
            f"❓ <b>Правила проживания</b>\n\n{html.escape(RULES_SHORT_TEXT)}",
            parse_mode="HTML",
            reply_markup=rules_gate_kb(),
        )
        return

    await message.answer(
        "Нажмите кнопку ниже, чтобы открыть бронирование 👇",
        reply_markup=booking_open_kb(),
    )


@dp.callback_query(F.data == "rules:accept")
async def rules_accept(callback: CallbackQuery):
    mark_rules_accepted(callback.from_user.id)
    await callback.answer("Подтверждение сохранено")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.answer(
        "✅ Спасибо. Теперь можете перейти к бронированию.",
        reply_markup=booking_open_kb(),
    )


@dp.callback_query(F.data == "rules:back")
async def rules_back(callback: CallbackQuery):
    await callback.answer()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.answer(
        "Хорошо. Когда будете готовы, нажмите «🏕️ Забронировать домик».",
        reply_markup=main_kb(),
    )


@dp.message(F.text == "📅 Мои брони")
async def show_active_bookings(message: Message):
    try:
        data = await api_post("/api/v2/bookings/active", {"tg_user_id": message.from_user.id})
    except Exception as e:
        await message.answer(f"Не удалось получить брони:\n{e}", reply_markup=main_kb())
        return

    items = data.get("items", [])
    if not items:
        await message.answer("У вас пока нет активных броней.", reply_markup=main_kb())
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
        "📞 Связаться с нами можно по кнопке ниже:",
        reply_markup=contact_support_kb(),
    )


@dp.message(F.text == "📍 Как добраться")
async def how_to_get(message: Message):
    kb = route_kb()
    if not kb:
        await message.answer(
            "Маршрут пока не настроен.",
            reply_markup=main_kb(),
        )
        return

    await message.answer(
        build_how_to_get_text(),
        parse_mode="HTML",
        reply_markup=kb,
        link_preview_options=no_preview(),
    )


@dp.message(F.text == "❓ Правила проживания")
async def rules(message: Message):
    await message.answer(
        f"❓ <b>Правила проживания</b>\n\n{html.escape(RULES_SHORT_TEXT)}",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )
    if RULES_PDF_PATH and os.path.exists(RULES_PDF_PATH):
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

    remember_booking_contact(
        created["booking_id"],
        tg_user_id=message.from_user.id,
        guest_name=message.from_user.full_name,
        guest_username=message.from_user.username,
        phone=message.contact.phone_number,
    )

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
async def cancel_booking(callback: CallbackQuery, state: FSMContext):
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

    state_data = await state.get_data()
    if state_data.get("receipt_booking_id") == booking_id:
        await state.update_data(receipt_booking_id=None)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await notify_admin_cancel(
        guest_tg_user_id=callback.from_user.id,
        guest_name=callback.from_user.full_name,
        guest_username=callback.from_user.username,
        booking_id=booking_id,
    )

    await callback.answer("Бронь отменена")
    await callback.message.answer(
        f"✅ Бронь <code>{result['booking_id']}</code> отменена.",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


@dp.callback_query(F.data.startswith("paid:"))
async def paid_click(callback: CallbackQuery, state: FSMContext):
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

    await state.update_data(receipt_booking_id=booking_id)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

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
async def on_receipt_photo(message: Message, state: FSMContext):
    state_data = await state.get_data()
    preferred_booking_id = state_data.get("receipt_booking_id")

    try:
        target = await find_receipt_target(message.from_user.id, preferred_booking_id)
    except Exception as e:
        await message.answer(f"Не удалось проверить брони:\n{e}", reply_markup=main_kb())
        return

    if not target:
        if preferred_booking_id is not None:
            await state.update_data(receipt_booking_id=None)
            await message.answer(
                "Не удалось определить бронь, к которой нужно прикрепить чек.\n"
                "Откройте «📅 Мои брони» и нажмите «✅ Я оплатил(а)» у нужной брони ещё раз.",
                reply_markup=main_kb(),
            )
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

    await state.update_data(receipt_booking_id=None)

    saved_contact = get_booking_contact(target["booking_id"]) or {}

    await notify_admin_receipt(
        guest_tg_user_id=saved_contact.get("tg_user_id") or message.from_user.id,
        guest_name=saved_contact.get("guest_name") or message.from_user.full_name,
        guest_username=saved_contact.get("guest_username") or message.from_user.username,
        phone=saved_contact.get("phone") or "—",
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
async def on_receipt_document(message: Message, state: FSMContext):
    state_data = await state.get_data()
    preferred_booking_id = state_data.get("receipt_booking_id")

    try:
        target = await find_receipt_target(message.from_user.id, preferred_booking_id)
    except Exception as e:
        await message.answer(f"Не удалось проверить брони:\n{e}", reply_markup=main_kb())
        return

    if not target:
        if preferred_booking_id is not None:
            await state.update_data(receipt_booking_id=None)
            await message.answer(
                "Не удалось определить бронь, к которой нужно прикрепить чек.\n"
                "Откройте «📅 Мои брони» и нажмите «✅ Я оплатил(а)» у нужной брони ещё раз.",
                reply_markup=main_kb(),
            )
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

    await state.update_data(receipt_booking_id=None)

    saved_contact = get_booking_contact(target["booking_id"]) or {}

    await notify_admin_receipt(
        guest_tg_user_id=saved_contact.get("tg_user_id") or message.from_user.id,
        guest_name=saved_contact.get("guest_name") or message.from_user.full_name,
        guest_username=saved_contact.get("guest_username") or message.from_user.username,
        phone=saved_contact.get("phone") or "—",
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

        current_text = callback.message.caption or callback.message.text or ""
        updated_text = replace_status_line(current_text, "Бронь подтверждена")

        try:
            if callback.message.photo or callback.message.document:
                await callback.message.edit_caption(
                    caption=updated_text,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            else:
                await callback.message.edit_text(
                    updated_text,
                    parse_mode="HTML",
                    reply_markup=None,
                )
        except Exception:
            logging.exception("Failed to update admin message after confirm")

        await callback.answer("Подтверждено")

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

        current_text = callback.message.caption or callback.message.text or ""
        updated_text = replace_status_line(current_text, "Бронь отклонена")

        try:
            if callback.message.photo or callback.message.document:
                await callback.message.edit_caption(
                    caption=updated_text,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            else:
                await callback.message.edit_text(
                    updated_text,
                    parse_mode="HTML",
                    reply_markup=None,
                )
        except Exception:
            logging.exception("Failed to update admin message after reject")

        await callback.answer("Отклонено")

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