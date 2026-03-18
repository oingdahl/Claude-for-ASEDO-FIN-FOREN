"""Tester för src/modules/clearing_accounts.py (Modul C)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.core.models import Company, Transaction, Voucher
from src.modules.clearing_accounts import ClearingAccountsModule

ORG = "556001-0001"

BASE_CONFIG = {
    "clearing_accounts": {
        "accounts": ["1630", "2393"],
        "owner_loan_accounts": ["2393"],
        "owner_loan_large_kr": 50_000,
        "old_balance_days": 90,
    }
}


def make_company(vouchers: list[Voucher], closing_balances: dict | None = None) -> Company:
    c = Company(org_nr=ORG, name="Testbolag AB", role="dotterbolag")
    c.vouchers = vouchers
    if closing_balances:
        c.closing_balances = {k: Decimal(str(v)) for k, v in closing_balances.items()}
    return c


def make_voucher(account: str, amount: int, d: date, text: str = "Test") -> Voucher:
    v = Voucher(series="A", number=1, date=d, text=text)
    v.transactions.append(Transaction(account=account, amount=amount))
    return v


# ---------------------------------------------------------------------------
# Ägarlån (2393)
# ---------------------------------------------------------------------------

def test_agarlan_60000_kr_ger_high() -> None:
    """Verifikation på 2393 med 60 000 kr → owner_loan HIGH."""
    v = make_voucher("2393", 6_000_000, date(2025, 6, 1))  # 60 000 kr i öre
    company = make_company([v])

    module = ClearingAccountsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    owner_loan = [f for f in findings if f.category == "owner_loan"]
    assert len(owner_loan) == 1
    assert owner_loan[0].risk_level == "HIGH"


def test_agarlan_litet_belopp_ger_medium() -> None:
    """Ägarlån under tröskeln (10 000 kr < 50 000 kr) → owner_loan MEDIUM."""
    v = make_voucher("2393", 1_000_000, date(2025, 6, 1))  # 10 000 kr
    company = make_company([v])

    module = ClearingAccountsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    owner_loan = [f for f in findings if f.category == "owner_loan"]
    assert len(owner_loan) == 1
    assert owner_loan[0].risk_level == "MEDIUM"


def test_agarlan_details_innehaller_ratt_info() -> None:
    """Finding.details ska ha konto, belopp och datum."""
    v = make_voucher("2393", 6_000_000, date(2025, 6, 1))
    company = make_company([v])

    module = ClearingAccountsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    f = findings[0]
    assert f.details["account"] == "2393"
    assert f.details["amount_kr"] == pytest.approx(60_000.0)
    assert f.details["org_nr"] == ORG


def test_agarlan_exakt_troskel_ger_high() -> None:
    """Exakt 50 000 kr = tröskeln → HIGH."""
    v = make_voucher("2393", 5_000_000, date(2025, 6, 1))  # 50 000 kr
    company = make_company([v])

    module = ClearingAccountsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    owner_loan = [f for f in findings if f.category == "owner_loan"]
    assert owner_loan[0].risk_level == "HIGH"


# ---------------------------------------------------------------------------
# Gammalt öppet saldo
# ---------------------------------------------------------------------------

def test_clearing_utan_rorelse_91_dagar_flaggas() -> None:
    """Konto 1630 utan rörelse i 91 dagar → old_open_balance."""
    ref = date(2025, 9, 30)
    last_move = ref - timedelta(days=91)

    v_old = make_voucher("1630", 500_000, last_move, "Gammal post")
    v_new = make_voucher("1930", 100_000, ref, "Ny bank")  # Annat konto = nyare ref-datum
    company = make_company([v_old, v_new])

    module = ClearingAccountsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    old_bal = [f for f in findings if f.category == "old_open_balance"]
    assert len(old_bal) >= 1
    assert old_bal[0].details["account"] == "1630"
    assert old_bal[0].details["days_without_movement"] == 91


def test_clearing_details_innehaller_dagar_och_belopp() -> None:
    """Finding.details ska ha konto, belopp och antal dagar utan rörelse."""
    ref = date(2025, 9, 30)
    v_old = make_voucher("1630", 1_000_000, ref - timedelta(days=95))
    v_ref = make_voucher("1930", 1, ref)
    company = make_company([v_old, v_ref])

    module = ClearingAccountsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    f = next(x for x in findings if x.category == "old_open_balance")
    assert "account" in f.details
    assert "balance_kr" in f.details
    assert "days_without_movement" in f.details
    assert f.details["days_without_movement"] == 95


def test_clearing_med_ny_rorelse_inga_fynd() -> None:
    """Clearing-konto med rörelse nyligen → inga old_open_balance-fynd."""
    today = date(2025, 9, 30)

    # Rörelse för 5 dagar sedan
    v1 = make_voucher("1630", 1_000_000, today - timedelta(days=5))
    v2 = make_voucher("1630", -1_000_000, today - timedelta(days=3))
    company = make_company([v1, v2])

    module = ClearingAccountsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    old_bal = [f for f in findings if f.category == "old_open_balance"]
    assert old_bal == []


def test_clearing_nollsaldo_utan_rorelse_inga_fynd() -> None:
    """Clearing-konto med saldo 0 (balanserat) → inga fynd även om gammalt."""
    ref = date(2025, 9, 30)
    # In + ut = 0
    v1 = make_voucher("1630", 500_000, ref - timedelta(days=100))
    v2 = make_voucher("1630", -500_000, ref - timedelta(days=100))
    v_ref = make_voucher("1930", 1, ref)
    company = make_company([v1, v2, v_ref])

    module = ClearingAccountsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    old_bal = [f for f in findings if f.category == "old_open_balance"]
    assert old_bal == []
