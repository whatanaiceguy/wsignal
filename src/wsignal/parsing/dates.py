from __future__ import annotations

import calendar
from datetime import date


def valid_publication_date(value: date | None) -> date | None:
    if value is None or value > date.today():
        return None
    return value


def subtract_months(value: date, months: int) -> date:
    month = value.month - max(0, months)
    year = value.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


