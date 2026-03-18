"""Tester för src/modules/bank_matching.py (Modul A)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.core.csv_importer import import_bank_csv
from src.core.models import (
    BankTransaction,
    Company,
    Finding,
    Transaction,
    Voucher,
)
from src.core.sie_parser import parse_sie4
from src.modules.bank_matching import BankMatchingModule

# ---------------------------------------------------------------------------
# Hjälpfunktioner för syntetisk testdata
# ---------------------------------------------------------------------------

BASE_DATE = date(2025, 6, 1)
ORG_NR = "556000-1234"
DEFAULT_CONFIG = {"matching": {"amount_tolerance_bank_kr": 50}}


def make_voucher(number: int, amount: int, account: str = "1930", days_offset: int = 0) -> Voucher:
    d = BASE_DATE + timedelta(days=days_offset)
    v = Voucher(
        series="A",
        number=number,
        date=d,
        text=f"Verifikation {number}",
    )
    v.transactions.append(
        Transaction(account=account, amount=amount, text=f"Bank {number}")
    )
    return v


def make_bank_tx(amount: int, days_offset: int = 0, text: str = "Betalning") -> BankTransaction:
    d = BASE_DATE + timedelta(days=days_offset)
    return BankTransaction(
        booking_date=d,
        value_date=d,
        text=text,
        transaction_type="Annan",
        amount=amount,
        running_balance=0,
        source_file="syntetisk",
        source_row=1,
    )


def make_company(vouchers: list[Voucher]) -> Company:
    c = Company(org_nr=ORG_NR, name="Testbolag AB", role="dotterbolag")
    c.vouchers = vouchers
    return c


# ---------------------------------------------------------------------------
# Starka matchningar
# ---------------------------------------------------------------------------

def test_starka_matcher_hittade() -> None:
    """10 exakta matchningar → 0 omatchade."""
    vouchers = [make_voucher(i, -(i * 10000)) for i in range(1, 11)]
    bank_txs = [make_bank_tx(-(i * 10000)) for i in range(1, 11)]

    company = make_company(vouchers)
    module = BankMatchingModule()
    findings = module.analyze([company], {ORG_NR: bank_txs}, DEFAULT_CONFIG)

    unmatched = [f for f in findings if f.category in ("unmatched_bank", "unmatched_book")]
    assert unmatched == [], f"Oväntade omatchade fynd: {[f.summary for f in unmatched]}"


def test_starka_matcher_med_datum_inom_3_dagar() -> None:
    """Datum ±3 dagar ska ge stark match."""
    vouchers = [make_voucher(1, -500000, days_offset=0)]
    bank_txs = [make_bank_tx(-500000, days_offset=3)]  # exakt 3 dagars skillnad

    module = BankMatchingModule()
    findings = module.analyze([make_company(vouchers)], {ORG_NR: bank_txs}, DEFAULT_CONFIG)

    unmatched = [f for f in findings if f.category in ("unmatched_bank", "unmatched_book")]
    assert unmatched == []


# ---------------------------------------------------------------------------
# Omatchade poster
# ---------------------------------------------------------------------------

def test_omatchade_bankposter_flaggas_high() -> None:
    """2 extra bankposter utan motpart → 2 HIGH unmatched_bank."""
    vouchers = [make_voucher(i, -(i * 10000)) for i in range(1, 11)]
    # 10 matchande + 2 extra
    bank_txs = [make_bank_tx(-(i * 10000)) for i in range(1, 11)]
    bank_txs.append(make_bank_tx(-9999900, text="Ingen match A"))
    bank_txs.append(make_bank_tx(-8888800, text="Ingen match B"))

    module = BankMatchingModule()
    findings = module.analyze([make_company(vouchers)], {ORG_NR: bank_txs}, DEFAULT_CONFIG)

    ub = [f for f in findings if f.category == "unmatched_bank"]
    assert len(ub) == 2
    assert all(f.risk_level == "HIGH" for f in ub)


def test_omatchade_bokposter_flaggas_high() -> None:
    """2 extra bokföringsposter utan motpart → 2 HIGH unmatched_book."""
    vouchers = [make_voucher(i, -(i * 10000)) for i in range(1, 11)]
    vouchers.append(make_voucher(11, -7777700))
    vouchers.append(make_voucher(12, -6666600))
    bank_txs = [make_bank_tx(-(i * 10000)) for i in range(1, 11)]

    module = BankMatchingModule()
    findings = module.analyze([make_company(vouchers)], {ORG_NR: bank_txs}, DEFAULT_CONFIG)

    ubk = [f for f in findings if f.category == "unmatched_book"]
    assert len(ubk) == 2
    assert all(f.risk_level == "HIGH" for f in ubk)


# ---------------------------------------------------------------------------
# Datumavvikelse
# ---------------------------------------------------------------------------

def test_datumavvikelse_over_5_dagar_flaggas_medium() -> None:
    """Match på belopp men 6 dagars datumskillnad → date_discrepancy MEDIUM."""
    vouchers = [make_voucher(1, -500000, days_offset=0)]
    bank_txs = [make_bank_tx(-500000, days_offset=6)]  # 6 dagars skillnad

    module = BankMatchingModule()
    findings = module.analyze([make_company(vouchers)], {ORG_NR: bank_txs}, DEFAULT_CONFIG)

    dd = [f for f in findings if f.category == "date_discrepancy"]
    assert len(dd) == 1
    assert dd[0].risk_level == "MEDIUM"
    assert dd[0].details["date_diff_days"] == 6


def test_datumavvikelse_exakt_5_dagar_ej_flaggad() -> None:
    """5 dagars skillnad ska INTE ge date_discrepancy (threshold är >5)."""
    vouchers = [make_voucher(1, -500000, days_offset=0)]
    bank_txs = [make_bank_tx(-500000, days_offset=5)]

    module = BankMatchingModule()
    findings = module.analyze([make_company(vouchers)], {ORG_NR: bank_txs}, DEFAULT_CONFIG)

    dd = [f for f in findings if f.category == "date_discrepancy"]
    assert dd == []


# ---------------------------------------------------------------------------
# Beloppstolerans
# ---------------------------------------------------------------------------

def test_tolerans_49_kr_ger_match() -> None:
    """49 kr diff (< 50 kr tolerans) → match, amount_discrepancy."""
    vouchers = [make_voucher(1, -500000)]  # -5000,00 kr
    bank_txs = [make_bank_tx(-504900)]     # -5049,00 kr (diff 49 kr)

    module = BankMatchingModule()
    findings = module.analyze([make_company(vouchers)], {ORG_NR: bank_txs}, DEFAULT_CONFIG)

    unmatched = [f for f in findings if f.category in ("unmatched_bank", "unmatched_book")]
    assert unmatched == [], "49 kr ska ge match (inom tolerans)"

    ad = [f for f in findings if f.category == "amount_discrepancy"]
    assert len(ad) == 1
    assert ad[0].details["amount_diff_oren"] == 4900


def test_tolerans_51_kr_ger_omatchad() -> None:
    """51 kr diff (> 50 kr tolerans) → unmatched på båda sidor."""
    vouchers = [make_voucher(1, -500000)]  # -5000,00 kr
    bank_txs = [make_bank_tx(-505100)]     # -5051,00 kr (diff 51 kr)

    module = BankMatchingModule()
    findings = module.analyze([make_company(vouchers)], {ORG_NR: bank_txs}, DEFAULT_CONFIG)

    unmatched = [f for f in findings if f.category in ("unmatched_bank", "unmatched_book")]
    assert len(unmatched) == 2, "51 kr diff ska ge omatchade poster"


def test_exakt_toleransgrans_50_kr_ger_match() -> None:
    """Exakt 50 kr diff (= tolerans) → match."""
    vouchers = [make_voucher(1, -500000)]
    bank_txs = [make_bank_tx(-505000)]  # diff = 5000 öre = 50 kr

    module = BankMatchingModule()
    findings = module.analyze([make_company(vouchers)], {ORG_NR: bank_txs}, DEFAULT_CONFIG)

    unmatched = [f for f in findings if f.category in ("unmatched_bank", "unmatched_book")]
    assert unmatched == []


# ---------------------------------------------------------------------------
# Konfigurering av tolerans
# ---------------------------------------------------------------------------

def test_anpassad_tolerans_respekteras() -> None:
    """Tolerans 10 kr: 11 kr diff → omatchad."""
    custom_config = {"matching": {"amount_tolerance_bank_kr": 10}}
    vouchers = [make_voucher(1, -500000)]
    bank_txs = [make_bank_tx(-501100)]  # diff 11 kr

    module = BankMatchingModule()
    findings = module.analyze([make_company(vouchers)], {ORG_NR: bank_txs}, custom_config)

    unmatched = [f for f in findings if f.category in ("unmatched_bank", "unmatched_book")]
    assert len(unmatched) == 2


# ---------------------------------------------------------------------------
# Icke-bankkonton ignoreras
# ---------------------------------------------------------------------------

def test_ej_bankkonto_ignoreras() -> None:
    """Verifikationer på konto 3000 ska inte tas med i avstämningen."""
    vouchers = [make_voucher(1, -500000, account="3000")]  # intäktskonto
    bank_txs = [make_bank_tx(-500000)]

    module = BankMatchingModule()
    findings = module.analyze([make_company(vouchers)], {ORG_NR: bank_txs}, DEFAULT_CONFIG)

    # Inga bokföringsposter på bankkonton → banktransaktionen omatchad
    ub = [f for f in findings if f.category == "unmatched_bank"]
    assert len(ub) == 1


# ---------------------------------------------------------------------------
# Integration mot riktiga testfiler
# ---------------------------------------------------------------------------

def test_integration_riktiga_filer_genererar_fynd() -> None:
    """SIE4 + CSV (olika datakällor) → fynd genereras (rimligt antal)."""
    company = parse_sie4("test_data/SIE4_Exempelfil.SE")
    transactions = import_bank_csv("test_data/Sophone_2025_transar.csv")

    module = BankMatchingModule()
    findings = module.analyze(
        [company],
        {company.org_nr: transactions},
        DEFAULT_CONFIG,
    )

    # Datakällorna matchar inte → vi förväntar oss fynd
    assert len(findings) > 0, "Inga fynd mot riktiga testfiler"

    # Alla fynd ska ha giltig risknivå
    valid_levels = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}
    for f in findings:
        assert f.risk_level in valid_levels

    # Räkna per kategori
    cats: dict[str, int] = {}
    for f in findings:
        cats[f.category] = cats.get(f.category, 0) + 1
    print(f"\nFynd per kategori mot riktiga filer: {cats}")


def test_integration_finding_ids_unika() -> None:
    """Alla finding_id ska vara unika inom en körning."""
    company = parse_sie4("test_data/SIE4_Exempelfil.SE")
    transactions = import_bank_csv("test_data/Sophone_2025_transar.csv")

    module = BankMatchingModule()
    findings = module.analyze([company], {company.org_nr: transactions}, DEFAULT_CONFIG)

    ids = [f.finding_id for f in findings]
    assert len(ids) == len(set(ids)), "Duplicerade finding_id"


def test_modul_egenskaper() -> None:
    """name och description ska vara satta."""
    m = BankMatchingModule()
    assert m.name == "bank_matching"
    assert len(m.description) > 10
