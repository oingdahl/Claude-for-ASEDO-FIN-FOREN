"""Gemensamma hjälpfunktioner för forensisk analys."""

from __future__ import annotations

from datetime import date, timedelta


def swedish_holidays(year: int) -> set[date]:
    """Beräknar svenska helgdagar för givet år.

    Använder Anonymous Gregorian algorithm för påskberäkning — inget extra
    paket krävs.

    Args:
        year: Kalenderår, t.ex. 2025.

    Returns:
        Mängd av date-objekt för samtliga svenska helgdagar under året.
    """
    # Fasta helgdagar
    fixed = [
        date(year, 1, 1),    # Nyårsdagen
        date(year, 1, 6),    # Trettondedag jul
        date(year, 5, 1),    # Första maj
        date(year, 6, 6),    # Nationaldagen
        date(year, 12, 24),  # Julafton
        date(year, 12, 25),  # Juldagen
        date(year, 12, 26),  # Annandag jul
        date(year, 12, 31),  # Nyårsafton
    ]

    # Påsk via Anonymous Gregorian algorithm
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(114 + h + ell - 7 * m, 31)
    easter_sunday = date(year, month, day + 1)

    moveable = [
        easter_sunday - timedelta(days=2),   # Långfredag
        easter_sunday,                        # Påskdagen
        easter_sunday + timedelta(days=1),    # Annandag påsk
        easter_sunday + timedelta(days=39),   # Kristi Himmelsfärd
    ]

    # Midsommarafton = fredag 19–25 juni
    june19 = date(year, 6, 19)
    days_to_friday = (4 - june19.weekday()) % 7
    moveable.append(june19 + timedelta(days=days_to_friday))

    return set(fixed + moveable)
