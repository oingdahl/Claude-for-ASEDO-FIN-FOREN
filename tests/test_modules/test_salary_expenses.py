"""Tester för src/modules/salary_expenses.py (Modul F)."""

from __future__ import annotations

from datetime import date

import pytest

from src.core.models import Company, Transaction, Voucher
from src.modules.salary_expenses import SalaryExpensesModule

ORG = "556001-0001"
BASE_CONFIG: dict = {
    "salary_expenses": {
        "salary_accounts": ["7010"],
        "salary_deviation_factor": 2.0,
        "salary_min_months": 3,
    }
}


def make_company(vouchers: list[Voucher]) -> Company:
    c = Company(org_nr=ORG, name="Testbolag AB", role="dotterbolag")
    c.vouchers = vouchers
    return c


def make_salary_voucher(amount_kr: int, year: int, month: int, n: int, account: str = "7010") -> Voucher:
    v = Voucher(series="A", number=n, date=date(year, month, 25), text="Lönekörning")
    v.transactions.append(Transaction(account=account, amount=amount_kr * 100, text="Lön"))
    return v


def make_expense_voucher(account: str, amount_kr: int, n: int, text: str = "") -> Voucher:
    v = Voucher(series="A", number=n, date=date(2025, 3, 15), text="Utgift")
    v.transactions.append(Transaction(account=account, amount=amount_kr * 100, text=text))
    return v


# ---------------------------------------------------------------------------
# Avvikande löner
# ---------------------------------------------------------------------------

def test_10x45000_plus_1x85000_ger_salary_deviation() -> None:
    """10 löner à 45 000 kr + 1 på 85 000 kr → salary_deviation HIGH."""
    vouchers = [make_salary_voucher(45_000, 2025, mo, mo) for mo in range(1, 11)]
    vouchers.append(make_salary_voucher(85_000, 2025, 11, 11))

    company = make_company(vouchers)
    module = SalaryExpensesModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    dev = [f for f in findings if f.category == "salary_deviation"]
    assert len(dev) >= 1
    assert dev[0].risk_level == "HIGH"
    # Det avvikande beloppet ska vara 85 000 kr
    assert any(abs(f.details["amount_kr"] - 85_000) < 1 for f in dev)


def test_salary_deviation_details_innehaller_mean_och_stdev() -> None:
    """Finding.details ska ha amount_kr, mean_kr och stdev_kr."""
    vouchers = [make_salary_voucher(45_000, 2025, mo, mo) for mo in range(1, 11)]
    vouchers.append(make_salary_voucher(85_000, 2025, 11, 11))

    company = make_company(vouchers)
    module = SalaryExpensesModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    f = next(x for x in findings if x.category == "salary_deviation")
    assert "mean_kr" in f.details
    assert "stdev_kr" in f.details
    assert f.details["stdev_kr"] > 0


def test_likartade_loner_inga_avvikande() -> None:
    """12 identiska löner → inga salary_deviation-fynd."""
    vouchers = [make_salary_voucher(45_000, 2025, mo, mo) for mo in range(1, 13)]
    company = make_company(vouchers)

    module = SalaryExpensesModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    dev = [f for f in findings if f.category == "salary_deviation"]
    assert dev == []


# ---------------------------------------------------------------------------
# Dubbelbetalning
# ---------------------------------------------------------------------------

def test_dubbel_lon_samma_manad_flaggas_high() -> None:
    """Lön 42 000 kr förekommer 2 gånger i januari → duplicate_salary HIGH."""
    v1 = make_salary_voucher(42_000, 2025, 1, 1)
    v2 = make_salary_voucher(42_000, 2025, 1, 2)
    v3 = make_salary_voucher(42_000, 2025, 2, 3)  # Februari — ingen dublett

    company = make_company([v1, v2, v3])
    module = SalaryExpensesModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    dupl = [f for f in findings if f.category == "duplicate_salary"]
    assert len(dupl) == 1
    assert dupl[0].risk_level == "HIGH"
    assert dupl[0].details["amount_kr"] == pytest.approx(42_000.0)
    assert dupl[0].details["month"] == 1


def test_olika_belopp_samma_manad_ej_dublett() -> None:
    """Två olika lönebelopp samma månad → ingen duplicate_salary."""
    v1 = make_salary_voucher(42_000, 2025, 1, 1)
    v2 = make_salary_voucher(43_000, 2025, 1, 2)

    company = make_company([v1, v2])
    module = SalaryExpensesModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    dupl = [f for f in findings if f.category == "duplicate_salary"]
    assert dupl == []


# ---------------------------------------------------------------------------
# Kreditkortsfakturor
# ---------------------------------------------------------------------------

def test_kreditkort_6550_21000_kr_ger_medium() -> None:
    """Konto 6550, 21 000 kr → credit_card_expense MEDIUM."""
    v = make_expense_voucher("6550", 21_000, 1)
    company = make_company([v])

    module = SalaryExpensesModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    cc = [f for f in findings if f.category == "credit_card_expense"]
    assert len(cc) == 1
    assert cc[0].risk_level == "MEDIUM"
    assert cc[0].details["amount_kr"] == pytest.approx(21_000.0)


def test_kreditkort_6550_19000_kr_inga_fynd() -> None:
    """Konto 6550, 19 000 kr → INGA fynd (under 20 000 kr)."""
    v = make_expense_voucher("6550", 19_000, 1)
    company = make_company([v])

    module = SalaryExpensesModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    cc = [f for f in findings if f.category == "credit_card_expense"]
    assert cc == []


def test_kreditkort_keyword_visa_flaggas() -> None:
    """Text 'Visa kortbetalning' på annat konto → credit_card_expense."""
    v = make_expense_voucher("6000", 25_000, 1, text="Visa kortbetalning")
    company = make_company([v])

    module = SalaryExpensesModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    cc = [f for f in findings if f.category == "credit_card_expense"]
    assert len(cc) == 1


def test_kreditkort_exakt_grans_20000_kr_flaggas() -> None:
    """Exakt 20 000 kr → credit_card_expense (>= threshold)."""
    v = make_expense_voucher("6550", 20_000, 1)
    company = make_company([v])

    module = SalaryExpensesModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    cc = [f for f in findings if f.category == "credit_card_expense"]
    assert len(cc) == 1
