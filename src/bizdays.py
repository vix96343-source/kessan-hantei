"""東証の営業日判定(土日・祝日・年末年始 12/31〜1/3 休場)。"""
from datetime import date, timedelta

import jpholiday


def is_business_day(d: date) -> bool:
    if d.weekday() >= 5 or jpholiday.is_holiday(d):
        return False
    if (d.month, d.day) in ((12, 31), (1, 1), (1, 2), (1, 3)):
        return False
    return True


def add_business_days(d: date, n: int) -> date:
    """n営業日後(n<0なら前)。d自体が休日でも数え方は同じ(n=1で次の営業日)。"""
    step = 1 if n >= 0 else -1
    for _ in range(abs(n)):
        d += timedelta(days=step)
        while not is_business_day(d):
            d += timedelta(days=step)
    return d


def next_business_day(d: date) -> date:
    return add_business_days(d, 1)
