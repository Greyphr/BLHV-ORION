"""All 'daily / weekly / monthly' boundaries use server time (Africa/Lagos, WAT, by default)."""
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("TIMEZONE", "Africa/Lagos"))


def now_local() -> datetime:
    return datetime.now(TZ)


def day_key() -> date:
    return now_local().date()


def week_key() -> str:
    y, w, _ = now_local().isocalendar()
    return f"{y}-W{w:02d}"


def month_key() -> str:
    return now_local().strftime("%Y-%m")
