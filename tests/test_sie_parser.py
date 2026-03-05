"""Tester för src/core/sie_parser.py mot test_data/SIE4_Exempelfil.SE."""

import re
from datetime import date
from decimal import Decimal

import pytest

from src.core.models import Company
from src.core.sie_parser import SIEParseError, parse_sie4

TESTFILE = "test_data/SIE4_Exempelfil.SE"


@pytest.fixture(scope="module")
def company() -> Company:
    """Parsad Company från exempelfilen – laddas en gång per test-session."""
    return parse_sie4(TESTFILE)


# ---------------------------------------------------------------------------
# Grundläggande metadata
# ---------------------------------------------------------------------------

def test_foretagsnamn(company: Company) -> None:
    assert company.name == "Övningsbolaget AB"


def test_orgnr(company: Company) -> None:
    assert company.org_nr == "555555-5555"


def test_checksumma(company: Company) -> None:
    assert company.source_checksum != ""
    assert len(company.source_checksum) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Antal verifikationer och transaktioner
# ---------------------------------------------------------------------------

def test_antal_verifikationer(company: Company) -> None:
    assert len(company.vouchers) == 295


def test_antal_transaktioner(company: Company) -> None:
    """Totalt antal transaktioner ska stämma med #TRANS i källfilen."""
    raw = open(TESTFILE, "rb").read().decode("cp437")
    expected = sum(1 for ln in raw.splitlines() if re.match(r"^\s*#TRANS\b", ln))
    actual = sum(len(v.transactions) for v in company.vouchers)
    assert actual == expected


# ---------------------------------------------------------------------------
# Kontoplan
# ---------------------------------------------------------------------------

def test_kontoplan_stickprov(company: Company) -> None:
    for konto in ("1910", "2641", "3041"):
        assert konto in company.chart_of_accounts, f"Konto {konto} saknas"


def test_kontoplan_antal(company: Company) -> None:
    assert len(company.chart_of_accounts) == 8


# ---------------------------------------------------------------------------
# IB / UB för konto 1930
# ---------------------------------------------------------------------------

def test_ib_1930(company: Company) -> None:
    ib = company.opening_balances["1930"]
    assert ib == Decimal("938311.64")
    assert int(ib * 100) == 93831164


def test_ub_1930(company: Company) -> None:
    ub = company.closing_balances["1930"]
    assert ub == Decimal("746686.19")
    assert int(ub * 100) == 74668619


# ---------------------------------------------------------------------------
# Dimensioner
# ---------------------------------------------------------------------------

def test_dimension_1(company: Company) -> None:
    assert 1 in company.dimensions
    assert company.dimensions[1].name == "Resultatenhet"


def test_dimension_6(company: Company) -> None:
    assert 6 in company.dimensions
    assert company.dimensions[6].name == "Projekt"


# ---------------------------------------------------------------------------
# Objektlista i TRANS
# ---------------------------------------------------------------------------

def test_objektlista_trans(company: Company) -> None:
    """Hitta en transaktion med {1 Nord 6 0001}."""
    found = False
    for v in company.vouchers:
        for t in v.transactions:
            if (1, "Nord") in t.objects and (6, "0001") in t.objects:
                found = True
                break
        if found:
            break
    assert found, "Ingen transaktion med objektlista {1 Nord 6 0001} hittades"


# ---------------------------------------------------------------------------
# Verifikation A 1 (Kaffebröd)
# ---------------------------------------------------------------------------

def test_ver_a1_metadata(company: Company) -> None:
    ver = next((v for v in company.vouchers if v.series == "A" and v.number == 1), None)
    assert ver is not None, "Verifikation A1 saknas"
    assert ver.date == date(2021, 1, 5)
    assert ver.text == "Kaffebröd"


def test_ver_a1_tre_transaktioner(company: Company) -> None:
    ver = next(v for v in company.vouchers if v.series == "A" and v.number == 1)
    assert len(ver.transactions) == 3


def test_ver_a1_balanserar(company: Company) -> None:
    ver = next(v for v in company.vouchers if v.series == "A" and v.number == 1)
    assert sum(t.amount for t in ver.transactions) == 0


# ---------------------------------------------------------------------------
# Alla verifikationer balanserar
# ---------------------------------------------------------------------------

def test_alla_verifikationer_balanserar(company: Company) -> None:
    for v in company.vouchers:
        total = sum(t.amount for t in v.transactions)
        assert abs(total) <= 1, (
            f"Verifikation {v.series}{v.number} ({v.date}) balanserar inte: "
            f"summa={total} öre"
        )


# ---------------------------------------------------------------------------
# Felhantering
# ---------------------------------------------------------------------------

def test_felaktig_fil_kastar_exception(tmp_path: pytest.TempPathFactory) -> None:
    bad = tmp_path / "bad.SE"
    bad.write_text(
        "#FNAMN \"Test\"\n#ORGNR 000000-0000\n"
        "#VER A 1 20210101 \"Test\"\n{\n   #TRANS 1910 {} 500\n}\n",
        encoding="latin-1",
    )
    with pytest.raises(SIEParseError):
        parse_sie4(str(bad))
