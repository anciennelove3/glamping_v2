from datetime import date
from pydantic import BaseModel, ConfigDict, Field


class UnitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    short_description: str | None = None
    full_description: str | None = None
    max_total_guests: int
    max_adults: int
    max_children: int
    active: bool


class TariffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    title: str
    description: str | None = None
    active: bool


class PaymentProfileOut(BaseModel):
    id: int
    title: str
    day_parity: str
    recipient_name: str
    bank_name: str
    personal_acc: str
    bic: str
    corr_acc: str


class PriceBreakdownItem(BaseModel):
    date: str
    weekday: int
    price_rub: int


class CalculateBookingRequest(BaseModel):
    unit_id: int = Field(..., ge=1)
    tariff_id: int = Field(..., ge=1)

    adults: int = Field(..., ge=1, le=2)
    children: int = Field(0, ge=0, le=3)
    extra_bed_count: int = Field(0, ge=0, le=3)

    check_in: date
    check_out: date


class CalculateBookingResponse(BaseModel):
    unit_id: int
    unit_title: str

    tariff_id: int
    tariff_title: str

    adults: int
    children: int
    total_guests: int
    extra_bed_count: int
    extra_bed_amount: int

    check_in: str
    check_out: str
    nights: int

    subtotal_amount: int
    total_amount: int
    prepay_amount: int

    payment_profile: PaymentProfileOut
    breakdown: list[PriceBreakdownItem]


class CreateBookingRequest(BaseModel):
    tg_user_id: int = Field(..., ge=1)
    phone: str = Field(..., min_length=5, max_length=32)

    telegram_name: str | None = None
    telegram_username: str | None = None

    unit_id: int = Field(..., ge=1)
    tariff_id: int = Field(..., ge=1)

    adults: int = Field(..., ge=1, le=2)
    children: int = Field(0, ge=0, le=3)
    extra_bed_count: int = Field(0, ge=0, le=3)

    check_in: date
    check_out: date


class CreateBookingResponse(BaseModel):
    booking_id: int
    status: str
    expires_at: str | None

    unit_id: int
    tariff_id: int

    total_amount: int
    prepay_amount: int

    payment_profile: PaymentProfileOut


class ActiveBookingsRequest(BaseModel):
    tg_user_id: int = Field(..., ge=1)


class ActiveBookingItem(BaseModel):
    booking_id: int
    status: str

    unit_id: int
    unit_title: str

    tariff_id: int
    tariff_title: str

    adults: int
    children: int
    total_guests: int
    extra_bed_count: int

    check_in: str
    check_out: str
    nights: int

    total_amount: int
    prepay_amount: int

    expires_at: str | None
    can_cancel: bool
    cancel_reason: str | None = None


class ActiveBookingsResponse(BaseModel):
    items: list[ActiveBookingItem]


class CancelBookingRequest(BaseModel):
    tg_user_id: int = Field(..., ge=1)
    booking_id: int = Field(..., ge=1)


class CancelBookingResponse(BaseModel):
    booking_id: int
    status: str
    cancelled_at: str | None
    message: str


class UserBookingActionRequest(BaseModel):
    tg_user_id: int = Field(..., ge=1)
    booking_id: int = Field(..., ge=1)


class AdminBookingActionRequest(BaseModel):
    booking_id: int = Field(..., ge=1)
    admin_tg_user_id: int | None = Field(default=None, ge=1)


class AdminBookingsListRequest(BaseModel):
    status_group: str = Field(..., pattern="^(awaiting_payment|awaiting_review|confirmed|closed)$")
    limit: int = Field(default=20, ge=1, le=100)


class AdminBookingItem(BaseModel):
    booking_id: int
    status: str

    tg_user_id: int
    phone: str
    telegram_name: str | None = None
    telegram_username: str | None = None

    unit_id: int
    unit_title: str

    tariff_id: int
    tariff_title: str

    adults: int
    children: int
    total_guests: int
    extra_bed_count: int

    check_in: str
    check_out: str
    nights: int

    total_amount: int
    prepay_amount: int

    created_at: str
    paid_clicked_at: str | None = None
    receipt_received_at: str | None = None
    confirmed_at: str | None = None
    cancelled_at: str | None = None
    expired_at: str | None = None

    expires_at: str | None = None
    receipt_attached: bool = False

    can_confirm: bool
    can_reject: bool
    can_cancel: bool


class AdminBookingsListResponse(BaseModel):
    items: list[AdminBookingItem]
    count: int


class BookingActionResponse(BaseModel):
    booking_id: int
    status: str
    expires_at: str | None = None
    message: str


class ReaperRunResponse(BaseModel):
    expired_awaiting_payment: int
    expired_awaiting_receipt: int
    expired_pending_review: int

class UnavailableDateRangeItem(BaseModel):
    check_in: str
    check_out: str


class UnavailableDatesRequest(BaseModel):
    unit_id: int = Field(..., ge=1)
    date_from: date
    date_to: date


class AdminWebappListRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


class UnavailableDatesResponse(BaseModel):
    unit_id: int
    date_from: str
    date_to: str
    items: list[UnavailableDateRangeItem]    