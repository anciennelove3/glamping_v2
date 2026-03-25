import os
from dotenv import load_dotenv

load_dotenv()

APP_TITLE = os.getenv("APP_TITLE", "Glamping API v2")
BOOKING_TZ = os.getenv("BOOKING_TZ", "Asia/Yekaterinburg")

DATABASE_URL = os.getenv(
    "DATABASE_URL_V2",
    "postgresql+asyncpg://glamping_user:strong_password@127.0.0.1:5432/glamping_v2",
)

MIN_LEAD_DAYS = int(os.getenv("MIN_LEAD_DAYS", "1"))
MIN_STAY_NIGHTS = int(os.getenv("MIN_STAY_NIGHTS", "1"))
MAX_STAY_NIGHTS = int(os.getenv("MAX_STAY_NIGHTS", "30"))

PREPAY_RATE = float(os.getenv("PREPAY_RATE", "0.5"))
EXTRA_BED_PRICE_RUB = int(os.getenv("EXTRA_BED_PRICE_RUB", "1000"))

MAX_ACTIVE_BOOKINGS = int(os.getenv("MAX_ACTIVE_BOOKINGS", "999"))

AWAITING_PAYMENT_MINUTES = int(os.getenv("AWAITING_PAYMENT_MINUTES", "30"))
MANAGER_REMINDER_HOURS = int(os.getenv("MANAGER_REMINDER_HOURS", "4"))
GUEST_PENDING_NOTIFY_HOURS = int(os.getenv("GUEST_PENDING_NOTIFY_HOURS", "12"))

REAPER_INTERVAL_SEC = int(os.getenv("REAPER_INTERVAL_SEC", "30"))
