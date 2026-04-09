from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import aiohttp
from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import and_, func, or_, select, update

from .config import (
    ADMIN_USER_IDS,
    APP_TITLE,
    AWAITING_PAYMENT_MINUTES,
    BOOKING_TZ,
    EXTRA_BED_PRICE_RUB,
    GUEST_PENDING_NOTIFY_HOURS,
    MANAGER_REMINDER_HOURS,
    MAX_ACTIVE_BOOKINGS,
    MAX_STAY_NIGHTS,
    MIN_LEAD_DAYS,
    MIN_STAY_NIGHTS,
    PREPAY_RATE,
    REAPER_INTERVAL_SEC,
    TG_BOT_TOKEN,
)
from .db import Base, SessionLocal, engine
from .models import (
    Booking,
    BookingStatus,
    DayParity,
    PaymentLog,
    PaymentLogStatus,
    PaymentProfile,
    Tariff,
    TariffWeekdayPrice,
    Unit,
    User,
)
from .schemas import (
    ActiveBookingItem,
    ActiveBookingsRequest,
    ActiveBookingsResponse,
    AdminBookingActionRequest,
    AdminBookingItem,
    AdminBookingsListRequest,
    AdminBookingsListResponse,
    AdminWebappCancelRequest,
    AdminWebappListRequest,
    BookingActionResponse,
    CalculateBookingRequest,
    CalculateBookingResponse,
    CancelBookingRequest,
    CancelBookingResponse,
    CreateBookingRequest,
    CreateBookingResponse,
    PaymentProfileOut,
    PriceBreakdownItem,
    ReaperRunResponse,
    TariffOut,
    UnitOut,
    UnavailableDateRangeItem,
    UnavailableDatesRequest,
    UnavailableDatesResponse,
    UserBookingActionRequest,
)
from .seed import seed_if_needed

app = FastAPI(title=APP_TITLE)

ADMIN_MESSAGES_PATH = "/root/glamping_v2/bot/admin_messages.json"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_local_date() -> date:
    try:
        tz = ZoneInfo(BOOKING_TZ)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).date()


def current_day_parity() -> DayParity:
    day_number = today_local_date().day
    return DayParity.EVEN if day_number % 2 == 0 else DayParity.ODD


async def get_current_payment_profile(session) -> PaymentProfile:
    parity = current_day_parity()

    r = await session.execute(
        select(PaymentProfile).where(
            PaymentProfile.day_parity == parity,
            PaymentProfile.active.is_(True),
        )
    )
    profile = r.scalar_one_or_none()

    if not profile:
        raise HTTPException(status_code=500, detail="Payment profile not configured")

    return profile


async def get_weekday_price(session, tariff_id: int, d: date) -> int:
    r = await session.execute(
        select(TariffWeekdayPrice).where(
            TariffWeekdayPrice.tariff_id == tariff_id,
            TariffWeekdayPrice.weekday == d.weekday(),
            TariffWeekdayPrice.is_available.is_(True),
        )
    )
    row = r.scalar_one_or_none()

    if not row:
        raise HTTPException(
            status_code=400,
            detail=f"No active tariff price for {d.isoformat()}",
        )

    return int(row.price_rub)


async def upsert_user(
    session,
    *,
    tg_user_id: int,
    phone: str,
    telegram_name: str | None,
    telegram_username: str | None,
) -> User:
    now_dt = now_utc()

    q = await session.execute(select(User).where(User.tg_user_id == tg_user_id))
    user = q.scalar_one_or_none()

    if user:
        user.phone = phone
        user.telegram_name = telegram_name
        user.telegram_username = telegram_username
        user.active = True
        user.updated_at = now_dt
        return user

    user = User(
        tg_user_id=tg_user_id,
        phone=phone,
        telegram_name=telegram_name,
        telegram_username=telegram_username,
        active=True,
        created_at=now_dt,
        updated_at=now_dt,
    )
    session.add(user)
    await session.flush()
    return user


def payment_recipient_snapshot(profile: PaymentProfile) -> str:
    return profile.recipient_name


async def get_payment_log(session, booking_id: int) -> PaymentLog | None:
    q = await session.execute(select(PaymentLog).where(PaymentLog.booking_id == booking_id))
    return q.scalar_one_or_none()


def validate_guest_mix(unit: Unit, adults: int, children: int, extra_bed_count: int) -> int:
    total_guests = adults + children

    if adults < 1:
        raise HTTPException(status_code=400, detail="At least one adult is required")

    if adults > unit.max_adults:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum adults for {unit.title}: {unit.max_adults}",
        )

    if children > unit.max_children:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum children for {unit.title}: {unit.max_children}",
        )

    if total_guests > unit.max_total_guests:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum total guests for {unit.title}: {unit.max_total_guests}",
        )

    if extra_bed_count > children:
        raise HTTPException(
            status_code=400,
            detail="extra_bed_count cannot be greater than children count",
        )

    return total_guests


def validate_dates(check_in: date, check_out: date) -> int:
    min_checkin = today_local_date() + timedelta(days=MIN_LEAD_DAYS)

    if check_in < min_checkin:
        raise HTTPException(
            status_code=400,
            detail=f"check_in must be >= {min_checkin.isoformat()}",
        )

    if check_out <= check_in:
        raise HTTPException(status_code=400, detail="check_out must be after check_in")

    nights = (check_out - check_in).days
    if nights < MIN_STAY_NIGHTS or nights > MAX_STAY_NIGHTS:
        raise HTTPException(
            status_code=400,
            detail=f"Length of stay must be between {MIN_STAY_NIGHTS} and {MAX_STAY_NIGHTS} nights",
        )

    return nights


async def build_booking_calculation(
    session,
    *,
    unit: Unit,
    tariff: Tariff,
    adults: int,
    children: int,
    extra_bed_count: int,
    check_in: date,
    check_out: date,
):
    total_guests = validate_guest_mix(unit, adults, children, extra_bed_count)
    nights = validate_dates(check_in, check_out)

    breakdown: list[PriceBreakdownItem] = []
    subtotal_amount = 0
    cur = check_in

    while cur < check_out:
        price = await get_weekday_price(session, tariff.id, cur)
        subtotal_amount += price
        breakdown.append(
            PriceBreakdownItem(
                date=cur.isoformat(),
                weekday=cur.weekday(),
                price_rub=price,
            )
        )
        cur += timedelta(days=1)

    extra_bed_amount = extra_bed_count * EXTRA_BED_PRICE_RUB
    total_amount = subtotal_amount + extra_bed_amount
    prepay_amount = int(total_amount * PREPAY_RATE)

    return {
        "total_guests": total_guests,
        "nights": nights,
        "breakdown": breakdown,
        "subtotal_amount": subtotal_amount,
        "extra_bed_amount": extra_bed_amount,
        "total_amount": total_amount,
        "prepay_amount": prepay_amount,
    }


def active_booking_condition(now_dt: datetime, today: date):
    return or_(
        and_(
            Booking.status == BookingStatus.AWAITING_PAYMENT,
            Booking.awaiting_payment_expires_at.is_not(None),
            Booking.awaiting_payment_expires_at > now_dt,
        ),
        Booking.status == BookingStatus.AWAITING_RECEIPT,
        Booking.status == BookingStatus.PENDING_REVIEW,
        and_(
            Booking.status == BookingStatus.CONFIRMED,
            Booking.check_out > today,
        ),
    )


def get_booking_expires_at(booking: Booking) -> datetime | None:
    if booking.status == BookingStatus.AWAITING_PAYMENT:
        return booking.awaiting_payment_expires_at
    return None


def booking_cancel_policy(booking: Booking, today: date) -> tuple[bool, str | None]:
    if booking.status in (BookingStatus.CANCELLED, BookingStatus.EXPIRED, BookingStatus.COMPLETED):
        return False, "Бронь уже неактивна."

    if booking.status == BookingStatus.CONFIRMED:
        if booking.check_in < today + timedelta(days=2):
            return False, "До заезда менее 2 суток. Для отмены свяжитесь с администратором."
        return True, None

    if booking.status in (
        BookingStatus.AWAITING_PAYMENT,
        BookingStatus.AWAITING_RECEIPT,
        BookingStatus.PENDING_REVIEW,
    ):
        return True, None

    return False, "Отмена недоступна для текущего статуса."


def admin_action_flags(status: BookingStatus) -> tuple[bool, bool, bool]:
    if status in (BookingStatus.AWAITING_RECEIPT, BookingStatus.PENDING_REVIEW):
        return True, True, True
    if status == BookingStatus.AWAITING_PAYMENT:
        return False, True, True
    if status == BookingStatus.CONFIRMED:
        return False, False, True
    return False, False, False


def admin_status_condition(status_group: str):
    if status_group == "awaiting_payment":
        return Booking.status == BookingStatus.AWAITING_PAYMENT

    if status_group == "awaiting_review":
        return Booking.status.in_((BookingStatus.AWAITING_RECEIPT, BookingStatus.PENDING_REVIEW))

    if status_group == "confirmed":
        return Booking.status == BookingStatus.CONFIRMED

    if status_group == "closed":
        return Booking.status.in_((BookingStatus.CANCELLED, BookingStatus.EXPIRED, BookingStatus.COMPLETED))

    raise HTTPException(status_code=400, detail="Unknown admin status group")


def _build_telegram_data_check_string(init_data: str) -> str:
    pairs = parse_qsl(init_data, keep_blank_values=True)
    filtered = [(k, v) for k, v in pairs if k != "hash"]
    filtered.sort(key=lambda item: item[0])
    return "\n".join(f"{k}={v}" for k, v in filtered)


def verify_telegram_webapp_init_data(init_data: str) -> int | None:
    if not init_data or not TG_BOT_TOKEN:
        return None

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.get("hash")
    if not received_hash:
        return None

    check_string = _build_telegram_data_check_string(init_data)
    secret_key = hmac.new(b"WebAppData", TG_BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    user_raw = pairs.get("user")
    if not user_raw:
        return None

    try:
        user = json.loads(user_raw)
        return int(user.get("id"))
    except Exception:
        return None


def require_admin_user(telegram_init_data: str | None) -> int:
    if not ADMIN_USER_IDS:
        raise HTTPException(status_code=500, detail="ADMIN_USER_IDS is not configured")

    user_id = verify_telegram_webapp_init_data(telegram_init_data or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Admin auth failed")

    if user_id not in ADMIN_USER_IDS:
        raise HTTPException(status_code=403, detail="Admin access denied")

    return user_id


def _load_admin_message_ref(booking_id: int) -> dict | None:
    try:
        with open(ADMIN_MESSAGES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(str(booking_id))
    except Exception:
        return None


def _save_admin_message_ref(booking_id: int, ref: dict) -> None:
    try:
        try:
            with open(ADMIN_MESSAGES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

        data[str(booking_id)] = ref

        with open(ADMIN_MESSAGES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def _telegram_api_call(method: str, payload: dict) -> tuple[bool, dict | None]:
    if not TG_BOT_TOKEN:
        return False, None

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                data = await resp.json(content_type=None)
                return resp.status == 200 and bool(data.get("ok")), data
    except Exception:
        return False, None


def _admin_cancelled_text(
    *,
    booking_id: int,
    unit_title: str,
    tariff_title: str,
    check_in: str,
    check_out: str,
    adults: int,
    children: int,
    extra_bed_count: int,
    total_amount: int,
    prepay_amount: int,
) -> str:
    return (
        "❌ <b>Бронь отменена администратором</b>\n\n"
        f"🆔 Бронь: <code>{booking_id}</code>\n"
        f"🏠 Вариант: <b>{unit_title}</b> — <b>{tariff_title}</b>\n"
        f"📅 Даты: <b>{check_in} — {check_out}</b>\n"
        f"👨 Взрослые: {adults}\n"
        f"🧒 Дети: {children}\n"
        f"➕ Доп. места: {extra_bed_count}\n"
        f"💰 Итого: <b>{total_amount} ₽</b>\n"
        f"🔻 Предоплата: <b>{prepay_amount} ₽</b>\n"
        f"📌 Статус: <b>Бронь отменена администратором</b>"
    )


async def update_admin_chat_booking_cancelled(
    *,
    booking_id: int,
    unit_title: str,
    tariff_title: str,
    check_in: str,
    check_out: str,
    adults: int,
    children: int,
    extra_bed_count: int,
    total_amount: int,
    prepay_amount: int,
) -> None:
    ref = _load_admin_message_ref(booking_id)
    if not ref:
        return

    chat_id = ref.get("chat_id")
    message_id = ref.get("message_id")

    if not chat_id or not message_id:
        return

    text = _admin_cancelled_text(
        booking_id=booking_id,
        unit_title=unit_title,
        tariff_title=tariff_title,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        children=children,
        extra_bed_count=extra_bed_count,
        total_amount=total_amount,
        prepay_amount=prepay_amount,
    )

    await _telegram_api_call(
        "deleteMessage",
        {
            "chat_id": chat_id,
            "message_id": message_id,
        },
    )

    ok, result = await _telegram_api_call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        },
    )

    if ok and isinstance(result, dict):
        msg = result.get("result") or {}
        new_message_id = msg.get("message_id")
        if new_message_id:
            _save_admin_message_ref(
                booking_id,
                {
                    "chat_id": chat_id,
                    "message_id": new_message_id,
                    "kind": "text",
                },
            )


async def notify_guest_booking_cancelled_by_admin(
    *,
    tg_user_id: int,
    booking_id: int,
    unit_title: str,
    check_in: str,
    check_out: str,
):
    if not TG_BOT_TOKEN or not tg_user_id:
        return

    text = (
        "❌ <b>Ваша бронь была отменена администратором.</b>\n\n"
        f"🆔 Номер брони: <code>{booking_id}</code>\n"
        f"🏠 Домик: <b>{unit_title}</b>\n"
        f"📅 Даты: <b>{check_in} — {check_out}</b>\n\n"
        "Если это произошло по ошибке или нужна помощь — свяжитесь с нами."
    )

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": tg_user_id,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                await resp.text()
    except Exception:
        pass


async def expire_stale_bookings() -> dict[str, int]:
    now_dt = now_utc()

    async with SessionLocal() as session:
        q1 = await session.execute(
            update(Booking)
            .where(
                Booking.status == BookingStatus.AWAITING_PAYMENT,
                Booking.awaiting_payment_expires_at.is_not(None),
                Booking.awaiting_payment_expires_at <= now_dt,
            )
            .values(
                status=BookingStatus.EXPIRED,
                expired_at=now_dt,
                updated_at=now_dt,
                awaiting_payment_expires_at=None,
            )
            .returning(Booking.id)
        )
        expired_payment = len(q1.fetchall())

        await session.commit()

        return {
            "expired_awaiting_payment": expired_payment,
            "expired_awaiting_receipt": 0,
            "expired_pending_review": 0,
        }


async def reaper_loop():
    while True:
        try:
            await expire_stale_bookings()
        except Exception:
            pass
        await asyncio.sleep(REAPER_INTERVAL_SEC)


@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_if_needed()
    asyncio.create_task(reaper_loop())


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": APP_TITLE,
        "version": "v2",
    }


@app.get("/")
async def root():
    return {"message": "Glamping v2 backend is running"}


@app.get("/api/v2/units", response_model=list[UnitOut])
async def list_units():
    async with SessionLocal() as session:
        r = await session.execute(
            select(Unit).where(Unit.active.is_(True)).order_by(Unit.id.asc())
        )
        return list(r.scalars().all())


@app.get("/api/v2/tariffs", response_model=list[TariffOut])
async def list_tariffs():
    async with SessionLocal() as session:
        r = await session.execute(
            select(Tariff).where(Tariff.active.is_(True)).order_by(Tariff.id.asc())
        )
        return list(r.scalars().all())


@app.get("/api/v2/payment-profiles/current", response_model=PaymentProfileOut)
async def current_payment_profile():
    async with SessionLocal() as session:
        profile = await get_current_payment_profile(session)

        return PaymentProfileOut(
            id=profile.id,
            title=profile.title,
            day_parity=profile.day_parity.value,
            recipient_name=profile.recipient_name,
            bank_name=profile.bank_name,
            personal_acc=profile.personal_acc,
            bic=profile.bic,
            corr_acc=profile.corr_acc,
        )


@app.post("/api/v2/bookings/calculate", response_model=CalculateBookingResponse)
async def calculate_booking(req: CalculateBookingRequest):
    async with SessionLocal() as session:
        ru = await session.execute(
            select(Unit).where(Unit.id == req.unit_id, Unit.active.is_(True))
        )
        unit = ru.scalar_one_or_none()
        if not unit:
            raise HTTPException(status_code=400, detail="Unknown unit_id")

        rt = await session.execute(
            select(Tariff).where(Tariff.id == req.tariff_id, Tariff.active.is_(True))
        )
        tariff = rt.scalar_one_or_none()
        if not tariff:
            raise HTTPException(status_code=400, detail="Unknown tariff_id")

        calc = await build_booking_calculation(
            session,
            unit=unit,
            tariff=tariff,
            adults=req.adults,
            children=req.children,
            extra_bed_count=req.extra_bed_count,
            check_in=req.check_in,
            check_out=req.check_out,
        )

        profile = await get_current_payment_profile(session)

        return CalculateBookingResponse(
            unit_id=unit.id,
            unit_title=unit.title,
            tariff_id=tariff.id,
            tariff_title=tariff.title,
            adults=req.adults,
            children=req.children,
            total_guests=calc["total_guests"],
            extra_bed_count=req.extra_bed_count,
            extra_bed_amount=calc["extra_bed_amount"],
            check_in=req.check_in.isoformat(),
            check_out=req.check_out.isoformat(),
            nights=calc["nights"],
            subtotal_amount=calc["subtotal_amount"],
            total_amount=calc["total_amount"],
            prepay_amount=calc["prepay_amount"],
            payment_profile=PaymentProfileOut(
                id=profile.id,
                title=profile.title,
                day_parity=profile.day_parity.value,
                recipient_name=profile.recipient_name,
                bank_name=profile.bank_name,
                personal_acc=profile.personal_acc,
                bic=profile.bic,
                corr_acc=profile.corr_acc,
            ),
            breakdown=calc["breakdown"],
        )


@app.post("/api/v2/bookings/unavailable", response_model=UnavailableDatesResponse)
async def bookings_unavailable(req: UnavailableDatesRequest):
    now_dt = now_utc()
    today = today_local_date()

    if req.date_to <= req.date_from:
        raise HTTPException(status_code=400, detail="date_to must be after date_from")

    async with SessionLocal() as session:
        ru = await session.execute(
            select(Unit).where(Unit.id == req.unit_id, Unit.active.is_(True))
        )
        unit = ru.scalar_one_or_none()
        if not unit:
            raise HTTPException(status_code=400, detail="Unknown unit_id")

        q = await session.execute(
            select(Booking.check_in, Booking.check_out)
            .where(
                Booking.unit_id == req.unit_id,
                Booking.check_in < req.date_to,
                Booking.check_out > req.date_from,
                active_booking_condition(now_dt, today),
            )
            .order_by(Booking.check_in.asc(), Booking.check_out.asc())
        )

        raw_ranges = [
            {"check_in": check_in, "check_out": check_out}
            for check_in, check_out in q.all()
        ]

        if not raw_ranges:
            return UnavailableDatesResponse(
                unit_id=req.unit_id,
                date_from=req.date_from.isoformat(),
                date_to=req.date_to.isoformat(),
                items=[],
            )

        merged: list[dict] = []
        current = raw_ranges[0].copy()

        for item in raw_ranges[1:]:
            if item["check_in"] <= current["check_out"]:
                if item["check_out"] > current["check_out"]:
                    current["check_out"] = item["check_out"]
            else:
                merged.append(current)
                current = item.copy()

        merged.append(current)

        return UnavailableDatesResponse(
            unit_id=req.unit_id,
            date_from=req.date_from.isoformat(),
            date_to=req.date_to.isoformat(),
            items=[
                UnavailableDateRangeItem(
                    check_in=item["check_in"].isoformat(),
                    check_out=item["check_out"].isoformat(),
                )
                for item in merged
            ],
        )


@app.post("/api/v2/bookings/create", response_model=CreateBookingResponse)
async def create_booking(req: CreateBookingRequest):
    now_dt = now_utc()
    today = today_local_date()

    async with SessionLocal() as session:
        ru = await session.execute(
            select(Unit).where(Unit.id == req.unit_id, Unit.active.is_(True))
        )
        unit = ru.scalar_one_or_none()
        if not unit:
            raise HTTPException(status_code=400, detail="Unknown unit_id")

        rt = await session.execute(
            select(Tariff).where(Tariff.id == req.tariff_id, Tariff.active.is_(True))
        )
        tariff = rt.scalar_one_or_none()
        if not tariff:
            raise HTTPException(status_code=400, detail="Unknown tariff_id")

        calc = await build_booking_calculation(
            session,
            unit=unit,
            tariff=tariff,
            adults=req.adults,
            children=req.children,
            extra_bed_count=req.extra_bed_count,
            check_in=req.check_in,
            check_out=req.check_out,
        )

        cnt_q = await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.tg_user_id == req.tg_user_id,
                active_booking_condition(now_dt, today),
            )
        )
        active_count = int(cnt_q.scalar() or 0)
        if active_count >= MAX_ACTIVE_BOOKINGS:
            raise HTTPException(status_code=409, detail="Active booking limit reached")

        overlap_q = await session.execute(
            select(Booking.id)
            .where(
                Booking.unit_id == req.unit_id,
                Booking.check_in < req.check_out,
                Booking.check_out > req.check_in,
                active_booking_condition(now_dt, today),
            )
            .limit(1)
        )
        if overlap_q.first() is not None:
            raise HTTPException(status_code=409, detail="Selected dates are already occupied")

        await upsert_user(
            session,
            tg_user_id=req.tg_user_id,
            phone=req.phone,
            telegram_name=req.telegram_name,
            telegram_username=req.telegram_username,
        )

        profile = await get_current_payment_profile(session)
        expires_at = now_dt + timedelta(minutes=AWAITING_PAYMENT_MINUTES)

        booking = Booking(
            tg_user_id=req.tg_user_id,
            telegram_name=req.telegram_name,
            telegram_username=req.telegram_username,
            phone=req.phone,
            unit_id=unit.id,
            tariff_id=tariff.id,
            payment_profile_id=profile.id,
            adults=req.adults,
            children=req.children,
            total_guests=calc["total_guests"],
            extra_bed_count=req.extra_bed_count,
            extra_bed_amount=calc["extra_bed_amount"],
            check_in=req.check_in,
            check_out=req.check_out,
            nights=calc["nights"],
            status=BookingStatus.AWAITING_PAYMENT,
            total_amount=calc["total_amount"],
            prepay_amount=calc["prepay_amount"],
            awaiting_payment_expires_at=expires_at,
            awaiting_receipt_expires_at=None,
            review_expires_at=None,
            created_at=now_dt,
            updated_at=now_dt,
        )

        session.add(booking)
        await session.flush()

        payment_log = PaymentLog(
            booking_id=booking.id,
            amount=booking.prepay_amount,
            recipient=payment_recipient_snapshot(profile),
            qr_code_sent_at=now_dt,
            payment_notified_at=None,
            receipt_attached=False,
            confirmed_by=None,
            confirmed_at=None,
            status=PaymentLogStatus.QR_SENT,
            created_at=now_dt,
            updated_at=now_dt,
        )
        session.add(payment_log)

        await session.commit()
        await session.refresh(booking)

        return CreateBookingResponse(
            booking_id=booking.id,
            status=booking.status.value,
            expires_at=expires_at.isoformat(),
            unit_id=booking.unit_id,
            tariff_id=booking.tariff_id,
            total_amount=booking.total_amount,
            prepay_amount=booking.prepay_amount,
            payment_profile=PaymentProfileOut(
                id=profile.id,
                title=profile.title,
                day_parity=profile.day_parity.value,
                recipient_name=profile.recipient_name,
                bank_name=profile.bank_name,
                personal_acc=profile.personal_acc,
                bic=profile.bic,
                corr_acc=profile.corr_acc,
            ),
        )


@app.post("/api/v2/bookings/active", response_model=ActiveBookingsResponse)
async def active_bookings(req: ActiveBookingsRequest):
    now_dt = now_utc()
    today = today_local_date()

    async with SessionLocal() as session:
        q = await session.execute(
            select(Booking, Unit, Tariff)
            .join(Unit, Unit.id == Booking.unit_id)
            .join(Tariff, Tariff.id == Booking.tariff_id)
            .where(
                Booking.tg_user_id == req.tg_user_id,
                active_booking_condition(now_dt, today),
            )
            .order_by(Booking.created_at.desc())
        )

        items: list[ActiveBookingItem] = []

        for booking, unit, tariff in q.all():
            can_cancel, cancel_reason = booking_cancel_policy(booking, today)
            expires_at = get_booking_expires_at(booking)

            items.append(
                ActiveBookingItem(
                    booking_id=booking.id,
                    status=booking.status.value,
                    unit_id=booking.unit_id,
                    unit_title=unit.title,
                    tariff_id=booking.tariff_id,
                    tariff_title=tariff.title,
                    adults=booking.adults,
                    children=booking.children,
                    total_guests=booking.total_guests,
                    extra_bed_count=booking.extra_bed_count,
                    check_in=booking.check_in.isoformat(),
                    check_out=booking.check_out.isoformat(),
                    nights=booking.nights,
                    total_amount=booking.total_amount,
                    prepay_amount=booking.prepay_amount,
                    expires_at=expires_at.isoformat() if expires_at else None,
                    can_cancel=can_cancel,
                    cancel_reason=cancel_reason,
                )
            )

        return ActiveBookingsResponse(items=items)


@app.post("/api/v2/bookings/cancel", response_model=CancelBookingResponse)
async def cancel_booking(req: CancelBookingRequest):
    today = today_local_date()
    now_dt = now_utc()

    async with SessionLocal() as session:
        q = await session.execute(
            select(Booking).where(
                Booking.id == req.booking_id,
                Booking.tg_user_id == req.tg_user_id,
            )
        )
        booking = q.scalar_one_or_none()

        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")

        can_cancel, reason = booking_cancel_policy(booking, today)
        if not can_cancel:
            raise HTTPException(status_code=409, detail=reason or "Booking cannot be cancelled")

        booking.status = BookingStatus.CANCELLED
        booking.cancelled_at = now_dt
        booking.updated_at = now_dt

        booking.awaiting_payment_expires_at = None
        booking.awaiting_receipt_expires_at = None
        booking.review_expires_at = None

        payment_log = await get_payment_log(session, booking.id)
        if payment_log:
            payment_log.status = PaymentLogStatus.CANCELLED
            payment_log.updated_at = now_dt

        await session.commit()

        return CancelBookingResponse(
            booking_id=booking.id,
            status=booking.status.value,
            cancelled_at=booking.cancelled_at.isoformat() if booking.cancelled_at else None,
            message="Booking cancelled successfully",
        )


@app.post("/api/v2/bookings/paid-click", response_model=BookingActionResponse)
async def booking_paid_click(req: UserBookingActionRequest):
    now_dt = now_utc()

    async with SessionLocal() as session:
        q = await session.execute(
            select(Booking).where(
                Booking.id == req.booking_id,
                Booking.tg_user_id == req.tg_user_id,
            )
        )
        booking = q.scalar_one_or_none()

        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")

        if booking.status == BookingStatus.CONFIRMED:
            return BookingActionResponse(
                booking_id=booking.id,
                status=booking.status.value,
                expires_at=None,
                message="Booking already confirmed",
            )

        if booking.status != BookingStatus.AWAITING_PAYMENT:
            raise HTTPException(status_code=409, detail=f"Invalid status: {booking.status.value}")

        if not booking.awaiting_payment_expires_at or booking.awaiting_payment_expires_at <= now_dt:
            raise HTTPException(status_code=409, detail="Payment window expired")

        booking.status = BookingStatus.AWAITING_RECEIPT
        booking.paid_clicked_at = now_dt
        booking.awaiting_payment_expires_at = None
        booking.awaiting_receipt_expires_at = None
        booking.updated_at = now_dt

        payment_log = await get_payment_log(session, booking.id)
        if payment_log:
            payment_log.payment_notified_at = now_dt
            payment_log.status = PaymentLogStatus.PAYMENT_NOTIFIED
            payment_log.updated_at = now_dt

        await session.commit()

        return BookingActionResponse(
            booking_id=booking.id,
            status=booking.status.value,
            expires_at=None,
            message="Waiting for optional receipt or manager confirmation",
        )


@app.post("/api/v2/bookings/receipt-uploaded", response_model=BookingActionResponse)
async def booking_receipt_uploaded(req: UserBookingActionRequest):
    now_dt = now_utc()

    async with SessionLocal() as session:
        q = await session.execute(
            select(Booking).where(
                Booking.id == req.booking_id,
                Booking.tg_user_id == req.tg_user_id,
            )
        )
        booking = q.scalar_one_or_none()

        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")

        if booking.status == BookingStatus.AWAITING_PAYMENT:
            if not booking.awaiting_payment_expires_at or booking.awaiting_payment_expires_at <= now_dt:
                raise HTTPException(status_code=409, detail="Payment window expired")
            booking.status = BookingStatus.AWAITING_RECEIPT
            booking.paid_clicked_at = booking.paid_clicked_at or now_dt
            booking.awaiting_payment_expires_at = None

            payment_log = await get_payment_log(session, booking.id)
            if payment_log:
                payment_log.payment_notified_at = payment_log.payment_notified_at or now_dt
                payment_log.status = PaymentLogStatus.PAYMENT_NOTIFIED
                payment_log.updated_at = now_dt

        if booking.status not in (BookingStatus.AWAITING_RECEIPT, BookingStatus.PENDING_REVIEW):
            raise HTTPException(status_code=409, detail=f"Invalid status: {booking.status.value}")

        booking.status = BookingStatus.PENDING_REVIEW
        booking.receipt_received_at = now_dt
        booking.awaiting_receipt_expires_at = None
        booking.review_expires_at = None
        booking.updated_at = now_dt

        payment_log = await get_payment_log(session, booking.id)
        if payment_log:
            payment_log.receipt_attached = True
            payment_log.status = PaymentLogStatus.RECEIPT_ATTACHED
            payment_log.updated_at = now_dt

        await session.commit()

        return BookingActionResponse(
            booking_id=booking.id,
            status=booking.status.value,
            expires_at=None,
            message="Receipt received, waiting for manager review",
        )


@app.post("/api/v2/admin/bookings/list", response_model=AdminBookingsListResponse)
async def admin_list_bookings(req: AdminBookingsListRequest):
    async with SessionLocal() as session:
        total_q = await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(admin_status_condition(req.status_group))
        )
        total_count = int(total_q.scalar() or 0)

        q = await session.execute(
            select(Booking, Unit, Tariff, PaymentLog)
            .join(Unit, Unit.id == Booking.unit_id)
            .join(Tariff, Tariff.id == Booking.tariff_id)
            .outerjoin(PaymentLog, PaymentLog.booking_id == Booking.id)
            .where(admin_status_condition(req.status_group))
            .order_by(Booking.created_at.desc())
            .limit(req.limit)
        )

        items: list[AdminBookingItem] = []

        for booking, unit, tariff, payment_log in q.all():
            can_confirm, can_reject, can_cancel = admin_action_flags(booking.status)
            expires_at = get_booking_expires_at(booking)

            items.append(
                AdminBookingItem(
                    booking_id=booking.id,
                    status=booking.status.value,
                    tg_user_id=booking.tg_user_id,
                    phone=booking.phone,
                    telegram_name=booking.telegram_name,
                    telegram_username=booking.telegram_username,
                    unit_id=booking.unit_id,
                    unit_title=unit.title,
                    tariff_id=booking.tariff_id,
                    tariff_title=tariff.title,
                    adults=booking.adults,
                    children=booking.children,
                    total_guests=booking.total_guests,
                    extra_bed_count=booking.extra_bed_count,
                    check_in=booking.check_in.isoformat(),
                    check_out=booking.check_out.isoformat(),
                    nights=booking.nights,
                    total_amount=booking.total_amount,
                    prepay_amount=booking.prepay_amount,
                    created_at=booking.created_at.isoformat(),
                    paid_clicked_at=booking.paid_clicked_at.isoformat() if booking.paid_clicked_at else None,
                    receipt_received_at=booking.receipt_received_at.isoformat() if booking.receipt_received_at else None,
                    confirmed_at=booking.confirmed_at.isoformat() if booking.confirmed_at else None,
                    cancelled_at=booking.cancelled_at.isoformat() if booking.cancelled_at else None,
                    expired_at=booking.expired_at.isoformat() if booking.expired_at else None,
                    expires_at=expires_at.isoformat() if expires_at else None,
                    receipt_attached=bool(payment_log.receipt_attached) if payment_log else False,
                    can_confirm=can_confirm,
                    can_reject=can_reject,
                    can_cancel=can_cancel,
                )
            )

        return AdminBookingsListResponse(items=items, count=total_count)


@app.post("/api/v2/admin/bookings/confirm", response_model=BookingActionResponse)
async def admin_confirm_booking(req: AdminBookingActionRequest):
    now_dt = now_utc()

    async with SessionLocal() as session:
        q = await session.execute(select(Booking).where(Booking.id == req.booking_id))
        booking = q.scalar_one_or_none()

        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")

        if booking.status == BookingStatus.CONFIRMED:
            return BookingActionResponse(
                booking_id=booking.id,
                status=booking.status.value,
                expires_at=None,
                message="Booking already confirmed",
            )

        if booking.status not in (BookingStatus.AWAITING_RECEIPT, BookingStatus.PENDING_REVIEW):
            raise HTTPException(status_code=409, detail=f"Invalid status: {booking.status.value}")

        booking.status = BookingStatus.CONFIRMED
        booking.confirmed_at = now_dt
        booking.awaiting_receipt_expires_at = None
        booking.review_expires_at = None
        booking.updated_at = now_dt

        payment_log = await get_payment_log(session, booking.id)
        if payment_log:
            payment_log.confirmed_by = req.admin_tg_user_id
            payment_log.confirmed_at = now_dt
            payment_log.status = PaymentLogStatus.CONFIRMED
            payment_log.updated_at = now_dt

        await session.commit()

        return BookingActionResponse(
            booking_id=booking.id,
            status=booking.status.value,
            expires_at=None,
            message="Booking confirmed",
        )


@app.post("/api/v2/admin/bookings/reject", response_model=BookingActionResponse)
async def admin_reject_booking(req: AdminBookingActionRequest):
    now_dt = now_utc()

    async with SessionLocal() as session:
        q = await session.execute(select(Booking).where(Booking.id == req.booking_id))
        booking = q.scalar_one_or_none()

        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")

        if booking.status in (BookingStatus.CANCELLED, BookingStatus.EXPIRED):
            return BookingActionResponse(
                booking_id=booking.id,
                status=booking.status.value,
                expires_at=None,
                message="Booking already closed",
            )

        if booking.status not in (
            BookingStatus.AWAITING_PAYMENT,
            BookingStatus.AWAITING_RECEIPT,
            BookingStatus.PENDING_REVIEW,
        ):
            raise HTTPException(status_code=409, detail=f"Invalid status: {booking.status.value}")

        booking.status = BookingStatus.CANCELLED
        booking.cancelled_at = now_dt
        booking.updated_at = now_dt
        booking.awaiting_payment_expires_at = None
        booking.awaiting_receipt_expires_at = None
        booking.review_expires_at = None

        payment_log = await get_payment_log(session, booking.id)
        if payment_log:
            payment_log.status = PaymentLogStatus.REJECTED
            payment_log.updated_at = now_dt

        await session.commit()

        return BookingActionResponse(
            booking_id=booking.id,
            status=booking.status.value,
            expires_at=None,
            message="Booking rejected by admin",
        )


@app.post("/api/v2/admin/webapp/bookings/list", response_model=AdminBookingsListResponse)
async def admin_webapp_list_bookings(
    req: AdminWebappListRequest,
    x_telegram_init_data: str | None = Header(default=None),
):
    require_admin_user(x_telegram_init_data)
    today = today_local_date()

    condition = and_(
        Booking.status == BookingStatus.CONFIRMED,
        Booking.check_out > today,
    )

    async with SessionLocal() as session:
        total_q = await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(condition)
        )
        total_count = int(total_q.scalar() or 0)

        q = await session.execute(
            select(Booking, Unit, Tariff, PaymentLog)
            .join(Unit, Unit.id == Booking.unit_id)
            .join(Tariff, Tariff.id == Booking.tariff_id)
            .outerjoin(PaymentLog, PaymentLog.booking_id == Booking.id)
            .where(condition)
            .order_by(Booking.check_in.asc(), Booking.created_at.desc())
            .limit(req.limit)
        )

        items: list[AdminBookingItem] = []

        for booking, unit, tariff, payment_log in q.all():
            items.append(
                AdminBookingItem(
                    booking_id=booking.id,
                    status=booking.status.value,
                    tg_user_id=booking.tg_user_id,
                    phone=booking.phone,
                    telegram_name=booking.telegram_name,
                    telegram_username=booking.telegram_username,
                    unit_id=booking.unit_id,
                    unit_title=unit.title,
                    tariff_id=booking.tariff_id,
                    tariff_title=tariff.title,
                    adults=booking.adults,
                    children=booking.children,
                    total_guests=booking.total_guests,
                    extra_bed_count=booking.extra_bed_count,
                    check_in=booking.check_in.isoformat(),
                    check_out=booking.check_out.isoformat(),
                    nights=booking.nights,
                    total_amount=booking.total_amount,
                    prepay_amount=booking.prepay_amount,
                    created_at=booking.created_at.isoformat(),
                    paid_clicked_at=booking.paid_clicked_at.isoformat() if booking.paid_clicked_at else None,
                    receipt_received_at=booking.receipt_received_at.isoformat() if booking.receipt_received_at else None,
                    confirmed_at=booking.confirmed_at.isoformat() if booking.confirmed_at else None,
                    cancelled_at=booking.cancelled_at.isoformat() if booking.cancelled_at else None,
                    expired_at=booking.expired_at.isoformat() if booking.expired_at else None,
                    expires_at=None,
                    receipt_attached=bool(payment_log.receipt_attached) if payment_log else False,
                    can_confirm=False,
                    can_reject=False,
                    can_cancel=True,
                )
            )

        return AdminBookingsListResponse(items=items, count=total_count)


@app.post("/api/v2/admin/webapp/bookings/cancel", response_model=BookingActionResponse)
async def admin_webapp_cancel_booking(
    req: AdminWebappCancelRequest,
    x_telegram_init_data: str | None = Header(default=None),
):
    require_admin_user(x_telegram_init_data)
    now_dt = now_utc()
    today = today_local_date()

    async with SessionLocal() as session:
        q = await session.execute(
            select(Booking, Unit, Tariff)
            .join(Unit, Unit.id == Booking.unit_id)
            .join(Tariff, Tariff.id == Booking.tariff_id)
            .where(Booking.id == req.booking_id)
        )
        row = q.first()

        if not row:
            raise HTTPException(status_code=404, detail="Booking not found")

        booking, unit, tariff = row

        if booking.status in (BookingStatus.CANCELLED, BookingStatus.EXPIRED, BookingStatus.COMPLETED):
            return BookingActionResponse(
                booking_id=booking.id,
                status=booking.status.value,
                expires_at=None,
                message="Booking already closed",
            )

        if booking.status != BookingStatus.CONFIRMED or booking.check_out <= today:
            raise HTTPException(status_code=409, detail="Only active confirmed bookings can be cancelled here")

        booking.status = BookingStatus.CANCELLED
        booking.cancelled_at = now_dt
        booking.updated_at = now_dt
        booking.awaiting_payment_expires_at = None
        booking.awaiting_receipt_expires_at = None
        booking.review_expires_at = None

        payment_log = await get_payment_log(session, booking.id)
        if payment_log:
            payment_log.status = PaymentLogStatus.CANCELLED
            payment_log.updated_at = now_dt

        tg_user_id = booking.tg_user_id
        booking_id = booking.id
        unit_title = unit.title
        tariff_title = tariff.title
        check_in = booking.check_in.isoformat()
        check_out = booking.check_out.isoformat()
        adults = booking.adults
        children = booking.children
        extra_bed_count = booking.extra_bed_count
        total_amount = booking.total_amount
        prepay_amount = booking.prepay_amount

        await session.commit()

    await update_admin_chat_booking_cancelled(
        booking_id=booking_id,
        unit_title=unit_title,
        tariff_title=tariff_title,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        children=children,
        extra_bed_count=extra_bed_count,
        total_amount=total_amount,
        prepay_amount=prepay_amount,
    )

    await notify_guest_booking_cancelled_by_admin(
        tg_user_id=tg_user_id,
        booking_id=booking_id,
        unit_title=unit_title,
        check_in=check_in,
        check_out=check_out,
    )

    return BookingActionResponse(
        booking_id=booking_id,
        status=BookingStatus.CANCELLED.value,
        expires_at=None,
        message="Booking cancelled by admin",
    )


@app.post("/api/v2/internal/reaper/run-now", response_model=ReaperRunResponse)
async def reaper_run_now():
    result = await expire_stale_bookings()
    return ReaperRunResponse(**result)