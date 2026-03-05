"""Tester för src/core/data_validator.py."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from src.core.csv_importer import import_bank_csv
from src.core.data_validator import (
    ValidationReport,
    validate_bank_csv,
    validate_sie4,
    print_validation_summary,
)
from src.core.sie_parser import parse_sie4

SIE_FILE = "test_data/SIE4_Exempelfil.SE"
CSV_FILE = "test_data/Sophone_2025_transar.csv"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sie_company():
    return parse_sie4(SIE_FILE)


@pytest.fixture(scope="module")
def bank_transactions():
    return import_bank_csv(CSV_FILE)


@pytest.fixture(scope="module")
def sie_report(sie_company):
    return validate_sie4(sie_company, SIE_FILE)


@pytest.fixture(scope="module")
def csv_report(bank_transactions):
    return validate_bank_csv(bank_transactions, CSV_FILE)


# ---------------------------------------------------------------------------
# SIE4-validering — glad stig
# ---------------------------------------------------------------------------

def test_sie_is_valid(sie_report: ValidationReport) -> None:
    assert sie_report.is_valid is True, f"SIE4-rapport har errors: {sie_report.errors}"


def test_sie_file_type(sie_report: ValidationReport) -> None:
    assert sie_report.file_type == "SIE4"


def test_sie_no_errors(sie_report: ValidationReport) -> None:
    assert sie_report.errors == []


def test_sie_checksum_not_empty(sie_report: ValidationReport) -> None:
    assert len(sie_report.checksum) == 64


def test_sie_parsed_records_match(sie_report: ValidationReport) -> None:
    assert sie_report.parsed_records == sie_report.total_records


def test_sie_details_contain_ver(sie_report: ValidationReport) -> None:
    assert "ver_expected" in sie_report.details
    assert sie_report.details["ver_parsed"] == sie_report.details["ver_expected"]


def test_sie_details_contain_trans(sie_report: ValidationReport) -> None:
    assert "trans_expected" in sie_report.details
    assert sie_report.details["trans_parsed"] == sie_report.details["trans_expected"]


def test_sie_details_contain_konto(sie_report: ValidationReport) -> None:
    assert "konto_expected" in sie_report.details
    assert sie_report.details["konto_parsed"] == sie_report.details["konto_expected"]


def test_sie_no_unbalanced_vouchers(sie_report: ValidationReport) -> None:
    assert sie_report.details.get("unbalanced_vouchers", 0) == 0


def test_sie_no_duplicate_vouchers(sie_report: ValidationReport) -> None:
    assert sie_report.details.get("duplicate_voucher_numbers", 0) == 0


# ---------------------------------------------------------------------------
# CSV-validering — glad stig
# ---------------------------------------------------------------------------

def test_csv_is_valid(csv_report: ValidationReport) -> None:
    assert csv_report.is_valid is True, f"CSV-rapport har errors: {csv_report.errors}"


def test_csv_file_type(csv_report: ValidationReport) -> None:
    assert csv_report.file_type == "CSV"


def test_csv_no_errors(csv_report: ValidationReport) -> None:
    assert csv_report.errors == []


def test_csv_checksum_not_empty(csv_report: ValidationReport) -> None:
    assert len(csv_report.checksum) == 64


def test_csv_parsed_rows_match(csv_report: ValidationReport) -> None:
    assert csv_report.parsed_records == csv_report.total_records == 263


def test_csv_no_balance_errors(csv_report: ValidationReport) -> None:
    assert csv_report.details.get("balance_errors", 0) == 0


def test_csv_no_null_dates(csv_report: ValidationReport) -> None:
    assert csv_report.details.get("null_date_rows", 0) == 0


# ---------------------------------------------------------------------------
# Felhantering — korrupt SIE4 (borttagen verifikation)
# ---------------------------------------------------------------------------

def test_sie_invalid_when_voucher_missing(sie_company, tmp_path: Path) -> None:
    """Rapport ska ha is_valid=False om en verifikation saknas."""
    corrupted = copy.deepcopy(sie_company)
    if not corrupted.vouchers:
        pytest.skip("Inga verifikationer i testfilen")
    corrupted.vouchers.pop()  # ta bort sista verifikationen

    report = validate_sie4(corrupted, SIE_FILE)
    assert report.is_valid is False
    assert any("Verifikationsantal" in e for e in report.errors)


def test_sie_invalid_when_unbalanced(sie_company, tmp_path: Path) -> None:
    """Rapport ska ha is_valid=False om en verifikation inte balanserar."""
    corrupted = copy.deepcopy(sie_company)
    if not corrupted.vouchers:
        pytest.skip("Inga verifikationer i testfilen")
    # Manipulera beloppet på första transaktionen
    v = corrupted.vouchers[0]
    if v.transactions:
        v.transactions[0].amount += 100  # +1 kr avvikelse

    report = validate_sie4(corrupted, SIE_FILE)
    assert report.is_valid is False
    assert any("Obalanserade" in e for e in report.errors)


# ---------------------------------------------------------------------------
# print_validation_summary (röktest)
# ---------------------------------------------------------------------------

def test_print_summary_runs(
    sie_report: ValidationReport,
    csv_report: ValidationReport,
    capsys: pytest.CaptureFixture,
) -> None:
    print_validation_summary([sie_report, csv_report])
    out = capsys.readouterr().out
    assert "DATAKVALITETSRAPPORT" in out
    assert "SIE4" in out
    assert "CSV" in out
    assert "TOTALT" in out
