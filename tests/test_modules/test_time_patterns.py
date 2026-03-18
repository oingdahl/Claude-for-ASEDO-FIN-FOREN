"""Tester för src/modules/time_patterns.py (Modul E)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.core.models import Company, FiscalYear, Transaction, Voucher
from src.modules.time_patterns import TimePatternsModule

ORG = "556001-0001"
BASE_CONFIG: dict = {}


def make_company(vouchers: list[Voucher], period_start: date, period_end: date) -> Company:
    c = Company(org_nr=ORG, name="Testbolag AB", role="dotterbolag")
    c.vouchers = vouchers
    c.fiscal_years = [FiscalYear(year_index=0, start_date=period_start, end_date=period_end)]
    return c


def make_voucher(d: date, number: int = 1, amount: int = 100_000, account: str = "1930") -> Voucher:
    v = Voucher(series="A", number=number, date=d, text=f"Ver {number}")
    v.transactions.append(Transaction(account=account, amount=amount))
    return v


# ---------------------------------------------------------------------------
# Helgtransaktioner
# ---------------------------------------------------------------------------

def test_lordag_2024_03_30_flaggas() -> None:
    """2024-03-30 är en lördag → weekend_transaction MEDIUM."""
    d = date(2024, 3, 30)
    assert d.weekday() == 5, "2024-03-30 ska vara lördag"
    v = make_voucher(d)
    company = make_company([v], date(2024, 1, 1), date(2024, 12, 31))

    module = TimePatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    wt = [f for f in findings if f.category == "weekend_transaction"]
    assert len(wt) == 1
    assert wt[0].risk_level == "MEDIUM"
    assert wt[0].details["date"] == "2024-03-30"


def test_langfredag_2024_flaggas() -> None:
    """2024-03-29 är Långfredag → weekend_transaction."""
    v = make_voucher(date(2024, 3, 29))
    company = make_company([v], date(2024, 1, 1), date(2024, 12, 31))

    module = TimePatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    wt = [f for f in findings if f.category == "weekend_transaction"]
    assert len(wt) == 1
    assert wt[0].details["reason"] == "helgdag"


def test_vanlig_vardag_ej_flaggad() -> None:
    """Måndag 2025-03-10 → inga helgfynd."""
    v = make_voucher(date(2025, 3, 10))
    company = make_company([v], date(2025, 1, 1), date(2025, 12, 31))

    module = TimePatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    wt = [f for f in findings if f.category == "weekend_transaction"]
    assert wt == []


# ---------------------------------------------------------------------------
# Bokslutskluster
# ---------------------------------------------------------------------------

def test_bokslutskluster_10_i_slutet_flaggas() -> None:
    """10 verifikationer i sista 5 dagarna, låg bakgrundsnivå → closing_cluster HIGH."""
    period_end = date(2024, 12, 31)
    period_start = date(2024, 1, 1)

    # Bakgrund: 1 verifikation per vecka under jan-dec 26 (~52 stycken)
    background = []
    d = period_start
    n = 1
    while d <= date(2024, 12, 26):
        background.append(make_voucher(d, n))
        d += timedelta(days=7)
        n += 1

    # Kluster: 10 verifikationer 27-31 dec (2 per dag)
    cluster = []
    for day_offset in range(5):
        dd = date(2024, 12, 27) + timedelta(days=day_offset)
        cluster.append(make_voucher(dd, 100 + day_offset * 2))
        cluster.append(make_voucher(dd, 100 + day_offset * 2 + 1))

    company = make_company(background + cluster, period_start, period_end)

    module = TimePatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    cc = [f for f in findings if f.category == "closing_cluster"]
    assert len(cc) == 1
    assert cc[0].risk_level == "HIGH"


def test_jamn_fordelning_ej_bokslutskluster() -> None:
    """Jämn fördelning av verifikationer → inga closing_cluster-fynd."""
    period_end = date(2024, 12, 31)
    period_start = date(2024, 1, 1)

    # 1 verifikation per vecka jämnt fördelat hela året
    vouchers = []
    d = period_start
    n = 1
    while d <= period_end:
        vouchers.append(make_voucher(d, n))
        d += timedelta(days=7)
        n += 1

    company = make_company(vouchers, period_start, period_end)

    module = TimePatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    cc = [f for f in findings if f.category == "closing_cluster"]
    assert cc == []


# ---------------------------------------------------------------------------
# Reverseringar
# ---------------------------------------------------------------------------

def test_reversering_nara_bokslut_flaggas() -> None:
    """T1=2024-12-20 (+50000 kr), T2=2024-12-28 (-50000 kr) nära bokslut → reversal HIGH."""
    period_end = date(2024, 12, 31)

    v1 = Voucher(series="A", number=1, date=date(2024, 12, 20), text="Original")
    v1.transactions.append(Transaction(account="1930", amount=5_000_000))

    v2 = Voucher(series="A", number=2, date=date(2024, 12, 28), text="Reversering")
    v2.transactions.append(Transaction(account="1930", amount=-5_000_000))

    company = make_company([v1, v2], date(2024, 1, 1), period_end)

    module = TimePatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    rev = [f for f in findings if f.category == "reversal"]
    assert len(rev) >= 1
    assert rev[0].risk_level == "HIGH"
    assert rev[0].details["original_date"] == "2024-12-20"
    assert rev[0].details["reversal_date"] == "2024-12-28"


def test_ej_reversering_om_for_langt_isär() -> None:
    """T1 och T2 mer än 30 dagar isär → ingen reversal-flagga."""
    period_end = date(2024, 12, 31)

    v1 = Voucher(series="A", number=1, date=date(2024, 12, 1), text="Original")
    v1.transactions.append(Transaction(account="1930", amount=5_000_000))

    # T2 är 31 dagar senare
    v2 = Voucher(series="B", number=1, date=date(2025, 1, 1), text="Sen post")
    v2.transactions.append(Transaction(account="1930", amount=-5_000_000))

    company = make_company([v1, v2], date(2024, 1, 1), period_end)

    module = TimePatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    rev = [f for f in findings if f.category == "reversal"]
    assert rev == []


# ---------------------------------------------------------------------------
# Aktivitetsluckor
# ---------------------------------------------------------------------------

def test_gap_20_dagar_mars_flaggas() -> None:
    """20 dagars gap i mars → activity_gap MEDIUM."""
    period_start = date(2025, 1, 1)
    period_end = date(2025, 12, 31)

    v1 = make_voucher(date(2025, 3, 1), 1)
    v2 = make_voucher(date(2025, 3, 22), 2)  # 21 dagars gap

    company = make_company([v1, v2], period_start, period_end)

    module = TimePatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    gaps = [f for f in findings if f.category == "activity_gap"]
    assert len(gaps) >= 1
    assert gaps[0].risk_level == "MEDIUM"
    assert gaps[0].details["gap_days"] == 21


def test_gap_20_dagar_juli_ej_flaggad() -> None:
    """20 dagars gap som startar i juli → INGA activity_gap-fynd (undantag)."""
    period_start = date(2025, 1, 1)
    period_end = date(2025, 12, 31)

    v1 = make_voucher(date(2025, 7, 1), 1)
    v2 = make_voucher(date(2025, 7, 22), 2)  # Gap startar i juli

    company = make_company([v1, v2], period_start, period_end)

    module = TimePatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    gaps = [f for f in findings if f.category == "activity_gap"]
    assert gaps == []


def test_gap_exakt_14_dagar_ej_flaggad() -> None:
    """Exakt 14 dagars gap (= threshold) → ingen flagga."""
    v1 = make_voucher(date(2025, 3, 1), 1)
    v2 = make_voucher(date(2025, 3, 15), 2)  # 14 dagar

    company = make_company([v1, v2], date(2025, 1, 1), date(2025, 12, 31))

    module = TimePatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    gaps = [f for f in findings if f.category == "activity_gap"]
    assert gaps == []
