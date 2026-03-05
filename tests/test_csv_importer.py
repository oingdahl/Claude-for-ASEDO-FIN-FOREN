"""Tester för src/core/csv_importer.py mot test_data/Sophone_2025_transar.csv."""

from datetime import date

import pytest

from src.core.models import BankTransaction
from src.core.csv_importer import CSVImportError, import_bank_csv

TESTFILE = "test_data/Sophone_2025_transar.csv"


@pytest.fixture(scope="module")
def transactions() -> list[BankTransaction]:
    """Importerade transaktioner — laddas en gång per test-session."""
    return import_bank_csv(TESTFILE)


# ---------------------------------------------------------------------------
# Antal och datumintervall
# ---------------------------------------------------------------------------

def test_antal_transaktioner(transactions: list[BankTransaction]) -> None:
    assert len(transactions) == 263


def test_datumintervall_nyast(transactions: list[BankTransaction]) -> None:
    assert transactions[0].booking_date == date(2025, 12, 30)


def test_datumintervall_äldst(transactions: list[BankTransaction]) -> None:
    assert transactions[-1].booking_date == date(2025, 1, 3)


# ---------------------------------------------------------------------------
# Rad 2 (index 0) — första dataraden
# ---------------------------------------------------------------------------

def test_rad2_datum(transactions: list[BankTransaction]) -> None:
    assert transactions[0].booking_date == date(2025, 12, 30)


def test_rad2_belopp_oren(transactions: list[BankTransaction]) -> None:
    assert transactions[0].amount == 3103600  # 31 036,00 kr


def test_rad2_typ(transactions: list[BankTransaction]) -> None:
    assert transactions[0].transaction_type == "Bankgirobetalning"


def test_rad2_saldo(transactions: list[BankTransaction]) -> None:
    assert transactions[0].running_balance == 61199605  # 611 996,05 kr


def test_rad2_source_row(transactions: list[BankTransaction]) -> None:
    assert transactions[0].source_row == 2  # rad 1 = header


# ---------------------------------------------------------------------------
# Rad 3 (index 1) — uttag
# ---------------------------------------------------------------------------

def test_rad3_uttag_negativt(transactions: list[BankTransaction]) -> None:
    assert transactions[1].amount == -8776800  # -87 768,00 kr


def test_rad3_source_row(transactions: list[BankTransaction]) -> None:
    assert transactions[1].source_row == 3


# ---------------------------------------------------------------------------
# Lönepost
# ---------------------------------------------------------------------------

def test_lonepost_finns(transactions: list[BankTransaction]) -> None:
    lön = [t for t in transactions if "LÖN" in t.text.upper() and t.transaction_type == "Lön"]
    assert len(lön) >= 1, "Ingen lönepost med typ 'Lön' hittades"


def test_lonepost_typ(transactions: list[BankTransaction]) -> None:
    lön = next(t for t in transactions if t.transaction_type == "Lön")
    assert lön.transaction_type == "Lön"
    assert "LÖN" in lön.text.upper()


# ---------------------------------------------------------------------------
# Checksumma / source_file
# ---------------------------------------------------------------------------

def test_source_file_satt(transactions: list[BankTransaction]) -> None:
    assert transactions[0].source_file != ""


def test_source_row_sticker_prov(transactions: list[BankTransaction]) -> None:
    """source_row ska vara radnummer i CSV (1-indexerat, header = rad 1)."""
    for i, tx in enumerate(transactions):
        assert tx.source_row == i + 2  # rad 2 = index 0, osv.


# ---------------------------------------------------------------------------
# Felhantering
# ---------------------------------------------------------------------------

def test_saknad_kolumn_kastar_exception(tmp_path: pytest.TempPathFactory) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "Fel;Kolumner;Här\n2025-01-01;2025-01-01;Test\n",
        encoding="utf-8",
    )
    with pytest.raises(CSVImportError, match="Saknade kolumner"):
        import_bank_csv(str(bad))


def test_tom_fil_kastar_exception(tmp_path: pytest.TempPathFactory) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(CSVImportError, match="Tom fil"):
        import_bank_csv(str(empty))
