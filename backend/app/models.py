from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class DayParity(str, enum.Enum):
    EVEN = "EVEN"
    ODD = "ODD"


class BookingStatus(str, enum.Enum):
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    AWAITING_RECEIPT = "AWAITING_RECEIPT"
    PENDING_REVIEW = "PENDING_REVIEW"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"


class PaymentLogStatus(str, enum.Enum):
    QR_SENT = "QR_SENT"
    PAYMENT_NOTIFIED = "PAYMENT_NOTIFIED"
    RECEIPT_ATTACHED = "RECEIPT_ATTACHED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    tg_user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    telegram_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Unit(Base):
    __tablename__ = "units"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(100), nullable=False)

    short_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    max_total_guests: Mapped[int] = mapped_column(Integer, nullable=False)
    max_adults: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    max_children: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Tariff(Base):
    __tablename__ = "tariffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class TariffWeekdayPrice(Base):
    __tablename__ = "tariff_weekday_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tariff_id: Mapped[int] = mapped_column(Integer, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # Mon=0 .. Sun=6
    price_rub: Mapped[int] = mapped_column(Integer, nullable=False)

    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PaymentProfile(Base):
    __tablename__ = "payment_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)

    day_parity: Mapped[DayParity] = mapped_column(
        Enum(DayParity),
        nullable=False,
        unique=True,
    )

    recipient_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    bank_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    personal_acc: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    bic: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    corr_acc: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    tg_user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    telegram_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), nullable=False)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id"), nullable=False)
    payment_profile_id: Mapped[int | None] = mapped_column(ForeignKey("payment_profiles.id"), nullable=True)

    adults: Mapped[int] = mapped_column(Integer, nullable=False)
    children: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_guests: Mapped[int] = mapped_column(Integer, nullable=False)

    extra_bed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra_bed_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    check_in: Mapped[date] = mapped_column(Date, nullable=False)
    check_out: Mapped[date] = mapped_column(Date, nullable=False)  # exclusive
    nights: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus),
        nullable=False,
        default=BookingStatus.AWAITING_PAYMENT,
    )

    total_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    prepay_amount: Mapped[int] = mapped_column(Integer, nullable=False)

    awaiting_payment_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    awaiting_receipt_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    paid_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    receipt_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaymentLog(Base):
    __tablename__ = "payment_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), nullable=False, unique=True, index=True)

    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)

    qr_code_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    receipt_attached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    confirmed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[PaymentLogStatus] = mapped_column(
        Enum(PaymentLogStatus),
        nullable=False,
        default=PaymentLogStatus.QR_SENT,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
