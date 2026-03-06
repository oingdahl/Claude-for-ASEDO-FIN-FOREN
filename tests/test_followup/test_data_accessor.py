"""Tester för src/followup/data_accessor.py."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.core.models import BankTransaction, Company, Finding, FiscalYear, Transaction, Voucher
from src.followup.data_accessor import DataAccessor


# ---------------------------------------------------------------------------
# Syntetisk testdata
# ---------------------------------------------------------------------------

ORG_A = "556000-0001"
ORG_B = "556000-0002"
ORG_C = "556000-0003"


def _make_company(org_nr: str, name: str) -> Company:
    c = Company(org_nr=org_nr, name=name, role="dotterbolag")
    c.fiscal_years.append(FiscalYear(0, date(2025, 1, 1), date(2025, 12, 31)))
    return c


def _add_voucher(company: Company, acct: str, amount_ore: int, d: date, text: str = "Test") -> None:
    v = Voucher(series="A", number=len(company.vouchers) + 1, date=d, text=text)
    v.transactions.append(Transaction(account=acct, amount=amount_ore))
    v.transactions.append(Transaction(account="2999", amount=-amount_ore))  # motkonto
    company.vouchers.append(v)


def _make_bank_tx(amount_ore: int, d: date = date(2025, 6, 1), text: str = "Bank") -> BankTransaction:
    return BankTransaction(
        booking_date=d, value_date=d, text=text,
        transaction_type="Annan", amount=amount_ore,
        running_balance=0, source_file="test.csv", source_row=1,
    )


def _make_finding(fid: str, org_nr: str, module: str = "test", risk: str = "HIGH") -> Finding:
    return Finding(
        finding_id=fid, module=module, companies=[org_nr],
        risk_level=risk, category="test_cat", summary=f"Fynd {fid}",
    )


def _build_accessor() -> tuple[DataAccessor, list[Company]]:
    """Bygger en DataAccessor med syntetiska bolag och transaktioner."""
    company_a = _make_company(ORG_A, "Alpha AB")
    company_b = _make_company(ORG_B, "Beta AB")

    # Konto 1910: 3 transaktioner
    _add_voucher(company_a, "1910", 100_00, date(2025, 1, 15), "Kaffebröd från café")
    _add_voucher(company_a, "1910", 250_00, date(2025, 2, 10), "Leverantörsfaktura")
    _add_voucher(company_a, "1910", -50_00, date(2025, 3, 5), "Återbetalning")

    # Konto 7010: löner
    for month in range(1, 13):
        _add_voucher(company_a, "7010", 45_000_00, date(2025, month, 25), "Lön mars")

    # Konto 1660: IC-fordran
    company_a.closing_balances["1660"] = Decimal("150000.00")
    company_b.closing_balances["2460"] = Decimal("-150000.00")

    # Beta AB: en transaktion med text "Konsult JM"
    _add_voucher(company_b, "6530", -45_000_00, date(2025, 7, 14), "Konsult JM faktura")

    bank_tx = {ORG_A: [_make_bank_tx(100_00)]}
    findings = [
        _make_finding("F001", ORG_A, "bank_matching", "HIGH"),
        _make_finding("F002", ORG_A, "time_patterns", "MEDIUM"),
        _make_finding("F003", ORG_B, "intercompany", "LOW"),
    ]
    accessor = DataAccessor([company_a, company_b], bank_tx, findings)
    return accessor, [company_a, company_b]


class TestGetTransactionsByAccount:
    def test_returnerar_ratt_antal(self):
        accessor, _ = _build_accessor()
        txs = accessor.get_transactions_by_account(ORG_A, "1910")
        assert len(txs) == 3

    def test_filtrerar_pa_datum(self):
        accessor, _ = _build_accessor()
        txs = accessor.get_transactions_by_account(
            ORG_A, "1910",
            start_date=date(2025, 2, 1),
            end_date=date(2025, 2, 28),
        )
        assert len(txs) == 1
        assert txs[0]["date"] == "2025-02-10"

    def test_belopp_i_kronor(self):
        # Testdata: 100_00 i Python = 10000 ören = 100 kr
        accessor, _ = _build_accessor()
        txs = accessor.get_transactions_by_account(ORG_A, "1910")
        amounts = {t["amount_kr"] for t in txs}
        assert 100.0 in amounts   # 10000 ören = 100 kr
        assert 250.0 in amounts   # 25000 ören = 250 kr
        assert -50.0 in amounts   # -5000 ören = -50 kr

    def test_okant_bolag_ger_tom_lista(self):
        accessor, _ = _build_accessor()
        assert accessor.get_transactions_by_account("OKÄND", "1910") == []


class TestGetTransactionsByText:
    def test_hittar_kaffebrods_transaktion(self):
        accessor, _ = _build_accessor()
        results = accessor.get_transactions_by_text("kaffebröd")
        assert len(results) >= 1
        assert any("Kaffebröd" in r["voucher_text"] for r in results)

    def test_case_insensitive(self):
        accessor, _ = _build_accessor()
        assert len(accessor.get_transactions_by_text("KAFFEBRÖD")) >= 1
        assert len(accessor.get_transactions_by_text("kaffebröd")) >= 1

    def test_konsult_jm_hittas(self):
        # Sökning returnerar alla transaktioner i vouchers där texten matchar
        # (inklusive motkonto) — verifiera att minst ett resultat är från Beta AB
        accessor, _ = _build_accessor()
        results = accessor.get_transactions_by_text("Konsult JM")
        assert len(results) >= 1
        assert all(r["company_name"] == "Beta AB" for r in results)

    def test_filterar_pa_bolag(self):
        accessor, _ = _build_accessor()
        results = accessor.get_transactions_by_text("konsult", company_org_nr=ORG_A)
        assert len(results) == 0  # Konsult JM är bara i Beta AB


class TestGetMonthlyTotals:
    def test_returnerar_12_manader(self):
        accessor, _ = _build_accessor()
        totals = accessor.get_monthly_totals(ORG_A, "7010")
        assert len(totals) == 12

    def test_korrekt_manadssumma(self):
        # 45_000_00 i Python = 4500000 ören = 45000 kr
        accessor, _ = _build_accessor()
        totals = accessor.get_monthly_totals(ORG_A, "7010")
        for entry in totals:
            assert entry["debit_kr"] == pytest.approx(45000.0)
            break  # Räcker att kontrollera första månaden

    def test_belopp_i_kronor_inte_oren(self):
        # 100_00 ören = 10000 ören = 100 kr (inte 10000)
        accessor, _ = _build_accessor()
        txs = accessor.get_transactions_by_account(ORG_A, "1910")
        # Beloppet 100 kr (10000 ören) ska returneras som 100.0, inte 10000
        small_tx = next(t for t in txs if t["amount_kr"] == 100.0)
        assert small_tx is not None

    def test_tom_lista_om_konto_saknas(self):
        accessor, _ = _build_accessor()
        totals = accessor.get_monthly_totals(ORG_A, "9999")
        assert totals == []


class TestTraceIntercompanyFlow:
    def test_hittar_matchande_belopp(self):
        """Belopp 45 000 kr ska hittas i Beta AB."""
        accessor, _ = _build_accessor()
        results = accessor.trace_intercompany_flow(amount_kr=45000.0, tolerance_kr=500)
        assert len(results) >= 1
        assert any(r["company_name"] == "Beta AB" for r in results)

    def test_tolerans_respekteras(self):
        accessor, _ = _build_accessor()
        # 45 000 kr ± 100 kr — ska hitta 45 000 kr-transaktionen
        results = accessor.trace_intercompany_flow(amount_kr=45000.0, tolerance_kr=100)
        assert len(results) >= 1

    def test_for_liten_tolerans_ger_inga_resultat(self):
        accessor, _ = _build_accessor()
        # Söker 99 000 kr — ingen transaktion med det beloppet
        results = accessor.trace_intercompany_flow(amount_kr=99000.0, tolerance_kr=0)
        assert results == []

    def test_syntetisk_data_tre_bolag(self):
        """Spårar ett belopp genom 3 bolag."""
        company_a = _make_company(ORG_A, "Alpha AB")
        company_b = _make_company(ORG_B, "Beta AB")
        company_c = _make_company(ORG_C, "Gamma AB")

        _add_voucher(company_a, "1660", -50_000_00, date(2025, 3, 1), "Lån till Beta")
        _add_voucher(company_b, "1930", 50_000_00, date(2025, 3, 2), "Insättning från Alpha")
        _add_voucher(company_b, "1660", -49_500_00, date(2025, 3, 3), "Lån till Gamma")
        _add_voucher(company_c, "1930", 49_500_00, date(2025, 3, 4), "Insättning")

        accessor = DataAccessor([company_a, company_b, company_c], {}, [])
        results = accessor.trace_intercompany_flow(amount_kr=50000.0, tolerance_kr=600)

        company_names = {r["company_name"] for r in results}
        assert "Alpha AB" in company_names
        assert "Beta AB" in company_names
        assert "Gamma AB" in company_names


class TestGetIntercompanyMatrix:
    def test_matris_innehaller_ic_saldon(self):
        accessor, _ = _build_accessor()
        matrix = accessor.get_intercompany_matrix()
        # Alpha AB har fordran 150 000 kr
        assert ORG_A in matrix
        assert matrix[ORG_A]["receivables_kr"] == pytest.approx(150000.0)

    def test_alla_belopp_i_kronor(self):
        accessor, _ = _build_accessor()
        matrix = accessor.get_intercompany_matrix()
        for org_nr, data in matrix.items():
            # Belopp ska vara rimliga kronbelopp (< 10 miljoner för testdata)
            assert abs(data.get("receivables_kr", 0)) < 10_000_000
            assert abs(data.get("payables_kr", 0)) < 10_000_000


class TestGetFynd:
    def test_get_findings_by_risk(self):
        accessor, _ = _build_accessor()
        high = accessor.get_findings_by_risk("HIGH")
        assert all(f.risk_level == "HIGH" for f in high)
        assert len(high) == 1

    def test_get_findings_by_company(self):
        accessor, _ = _build_accessor()
        company_a_findings = accessor.get_findings_by_company(ORG_A)
        assert all(ORG_A in f.companies for f in company_a_findings)

    def test_get_findings_by_module(self):
        accessor, _ = _build_accessor()
        tp_findings = accessor.get_findings_by_module("time_patterns")
        assert len(tp_findings) == 1
        assert tp_findings[0].finding_id == "F002"

    def test_get_findings_by_id(self):
        accessor, _ = _build_accessor()
        results = accessor.get_findings_by_id(["F001", "F003"])
        ids = {f.finding_id for f in results}
        assert ids == {"F001", "F003"}


class TestCompareAccountsAcrossCompanies:
    def test_jämförelse_konto_1910(self):
        accessor, _ = _build_accessor()
        result = accessor.compare_accounts_across_companies("1910")
        assert ORG_A in result
        assert result[ORG_A]["transaction_count"] == 3

    def test_belopp_i_kronor(self):
        # 100_00 + 250_00 ören = 35000 ören = 350 kr (inte 350 ören = 3.50 kr)
        accessor, _ = _build_accessor()
        result = accessor.compare_accounts_across_companies("1910")
        # Total debet: 10000 + 25000 = 35000 ören = 350 kr
        assert result[ORG_A]["total_debit_kr"] == pytest.approx(350.0)
