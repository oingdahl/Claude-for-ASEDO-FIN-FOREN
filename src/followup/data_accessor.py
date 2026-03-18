"""Strukturerad tillgång till analysdata för uppföljningsfrågor.

Alla belopp returneras i kronor (float) — inte ören.
Alla metoder returnerar dicts/listor (ej dataclass-objekt) för enkel JSON-serialisering.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from src.core.models import BankTransaction, Company, Finding


def _ore_to_kr(ore: int) -> float:
    return round(ore / 100, 2)


class DataAccessor:
    """Ger strukturerad tillgång till all parsad analysdata."""

    def __init__(
        self,
        companies: list[Company],
        bank_transactions: dict[str, list[BankTransaction]],
        findings: list[Finding],
    ) -> None:
        self._companies = companies
        self._bank_tx = bank_transactions
        self._findings = findings
        # Index: org_nr → Company
        self._by_org: dict[str, Company] = {c.org_nr: c for c in companies}
        # Index: lower-cased name → org_nr
        self._name_index: dict[str, str] = {c.name.lower(): c.org_nr for c in companies}

    # ------------------------------------------------------------------
    # Intern hjälpmetod: resolve org_nr from name or org_nr string
    # ------------------------------------------------------------------

    def _resolve_org(self, company: str | None) -> str | None:
        """Returnerar org_nr för en sträng som kan vara namn eller org_nr."""
        if not company:
            return None
        if company in self._by_org:
            return company
        return self._name_index.get(company.lower())

    # ------------------------------------------------------------------
    # Transaktionssökning
    # ------------------------------------------------------------------

    def get_transactions_by_account(
        self,
        company_org_nr: str,
        account: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict]:
        """Hämta alla transaktioner på ett konto, valfritt filtrerat på period."""
        company = self._by_org.get(company_org_nr)
        if not company:
            return []
        result = []
        for voucher in company.vouchers:
            d = voucher.date
            if start_date and d < start_date:
                continue
            if end_date and d > end_date:
                continue
            for tx in voucher.transactions:
                if tx.account == account:
                    result.append({
                        "date": str(d),
                        "voucher_series": voucher.series,
                        "voucher_number": voucher.number,
                        "voucher_text": voucher.text,
                        "account": tx.account,
                        "amount_kr": _ore_to_kr(tx.amount),
                        "text": tx.text or "",
                        "org_nr": company_org_nr,
                        "company_name": company.name,
                    })
        result.sort(key=lambda r: r["date"])
        return result

    def get_transactions_by_amount(
        self,
        amount_min_kr: float,
        amount_max_kr: float,
        company_org_nr: str | None = None,
    ) -> list[dict]:
        """Hämta transaktioner inom ett beloppsintervall, valfritt per bolag."""
        min_ore = int(amount_min_kr * 100)
        max_ore = int(amount_max_kr * 100)
        companies = (
            [self._by_org[company_org_nr]]
            if company_org_nr and company_org_nr in self._by_org
            else self._companies
        )
        result = []
        for company in companies:
            for voucher in company.vouchers:
                for tx in voucher.transactions:
                    if min_ore <= abs(tx.amount) <= max_ore:
                        result.append({
                            "date": str(voucher.date),
                            "voucher_series": voucher.series,
                            "voucher_number": voucher.number,
                            "voucher_text": voucher.text,
                            "account": tx.account,
                            "amount_kr": _ore_to_kr(tx.amount),
                            "text": tx.text or "",
                            "org_nr": company.org_nr,
                            "company_name": company.name,
                        })
        result.sort(key=lambda r: r["date"])
        return result

    def get_transactions_by_text(
        self,
        search_text: str,
        company_org_nr: str | None = None,
    ) -> list[dict]:
        """Sök transaktioner där verifikationstext matchar (case-insensitive)."""
        needle = search_text.lower()
        companies = (
            [self._by_org[company_org_nr]]
            if company_org_nr and company_org_nr in self._by_org
            else self._companies
        )
        result = []
        for company in companies:
            for voucher in company.vouchers:
                voucher_text = (voucher.text or "").lower()
                for tx in voucher.transactions:
                    tx_text = (tx.text or "").lower()
                    if needle in voucher_text or needle in tx_text:
                        result.append({
                            "date": str(voucher.date),
                            "voucher_series": voucher.series,
                            "voucher_number": voucher.number,
                            "voucher_text": voucher.text,
                            "account": tx.account,
                            "amount_kr": _ore_to_kr(tx.amount),
                            "text": tx.text or "",
                            "org_nr": company.org_nr,
                            "company_name": company.name,
                        })
        result.sort(key=lambda r: r["date"])
        return result

    def get_transactions_by_counterparty(self, counterparty: str) -> list[dict]:
        """Sök transaktioner mot en motpart i verifikationstext och banktext, över alla bolag."""
        return self.get_transactions_by_text(counterparty)

    # ------------------------------------------------------------------
    # Bolagsdata
    # ------------------------------------------------------------------

    def get_company_summary(self, company_org_nr: str) -> dict:
        """Sammanfattning av ett bolag."""
        company = self._by_org.get(company_org_nr)
        if not company:
            return {}
        total_tx = sum(len(v.transactions) for v in company.vouchers)
        total_amount = sum(
            abs(tx.amount) for v in company.vouchers for tx in v.transactions
        )
        findings_for_company = [
            f for f in self._findings if company_org_nr in f.companies
        ]
        by_risk: dict[str, int] = defaultdict(int)
        for f in findings_for_company:
            by_risk[f.risk_level] += 1

        return {
            "org_nr": company.org_nr,
            "name": company.name,
            "role": company.role,
            "voucher_count": len(company.vouchers),
            "transaction_count": total_tx,
            "total_turnover_kr": _ore_to_kr(total_amount),
            "account_count": len(company.chart_of_accounts),
            "findings_total": len(findings_for_company),
            "findings_by_risk": dict(by_risk),
        }

    def get_account_balances(
        self,
        company_org_nr: str,
        accounts: list[str] | None = None,
    ) -> dict:
        """Hämta IB/UB/resultat per konto för ett bolag."""
        company = self._by_org.get(company_org_nr)
        if not company:
            return {}
        all_accounts = accounts or list(
            set(company.opening_balances) | set(company.closing_balances) | set(company.results)
        )
        return {
            acct: {
                "ib_kr": _ore_to_kr(int(company.opening_balances.get(acct, 0) * 100)),
                "ub_kr": _ore_to_kr(int(company.closing_balances.get(acct, 0) * 100)),
                "result_kr": _ore_to_kr(int(company.results.get(acct, 0) * 100)),
            }
            for acct in sorted(all_accounts)
        }

    def get_intercompany_matrix(self) -> dict:
        """Matris över alla koncerninterna saldon."""
        ic_receivables = {"1660", "1661", "1662", "1663", "1664", "1665",
                          "1666", "1667", "1668", "1669"}
        ic_payables = {"2460", "2461", "2462", "2463", "2464", "2465",
                       "2466", "2467", "2468", "2469"}

        matrix: dict[str, dict[str, Any]] = {}
        for company in self._companies:
            rec_total = sum(
                int(v * 100)
                for acct, v in company.closing_balances.items()
                if acct in ic_receivables
            )
            pay_total = sum(
                int(v * 100)
                for acct, v in company.closing_balances.items()
                if acct in ic_payables
            )
            if rec_total or pay_total:
                matrix[company.org_nr] = {
                    "name": company.name,
                    "receivables_kr": _ore_to_kr(rec_total),
                    "payables_kr": _ore_to_kr(abs(pay_total)),
                    "net_kr": _ore_to_kr(rec_total + pay_total),
                }
        return matrix

    # ------------------------------------------------------------------
    # Fyndsökning
    # ------------------------------------------------------------------

    def get_findings_by_id(self, finding_ids: list[str]) -> list[Finding]:
        id_set = set(finding_ids)
        return [f for f in self._findings if f.finding_id in id_set]

    def get_findings_by_company(self, company_org_nr: str) -> list[Finding]:
        return [f for f in self._findings if company_org_nr in f.companies]

    def get_findings_by_module(self, module_name: str) -> list[Finding]:
        return [f for f in self._findings if f.module == module_name]

    def get_findings_by_risk(self, risk_level: str) -> list[Finding]:
        return [f for f in self._findings if f.risk_level == risk_level.upper()]

    # ------------------------------------------------------------------
    # Tidsserier
    # ------------------------------------------------------------------

    def get_monthly_totals(self, company_org_nr: str, account: str) -> list[dict]:
        """Månatliga summor (debet, kredit, netto) för ett konto."""
        company = self._by_org.get(company_org_nr)
        if not company:
            return []
        monthly: dict[str, dict[str, int]] = defaultdict(lambda: {"debit": 0, "credit": 0})
        for voucher in company.vouchers:
            month_key = voucher.date.strftime("%Y-%m")
            for tx in voucher.transactions:
                if tx.account == account:
                    if tx.amount >= 0:
                        monthly[month_key]["debit"] += tx.amount
                    else:
                        monthly[month_key]["credit"] += tx.amount
        result = []
        for month in sorted(monthly):
            d = monthly[month]["debit"]
            c = monthly[month]["credit"]
            result.append({
                "month": month,
                "debit_kr": _ore_to_kr(d),
                "credit_kr": _ore_to_kr(c),
                "net_kr": _ore_to_kr(d + c),
            })
        return result

    def get_transaction_timeline(
        self,
        company_org_nr: str | None = None,
        account: str | None = None,
        counterparty: str | None = None,
    ) -> list[dict]:
        """Kronologisk tidslinje av transaktioner med valfria filter."""
        companies = (
            [self._by_org[company_org_nr]]
            if company_org_nr and company_org_nr in self._by_org
            else self._companies
        )
        counterparty_lower = counterparty.lower() if counterparty else None
        result = []
        for company in companies:
            for voucher in company.vouchers:
                for tx in voucher.transactions:
                    if account and tx.account != account:
                        continue
                    if counterparty_lower:
                        haystack = (
                            (voucher.text or "").lower() + " " + (tx.text or "").lower()
                        )
                        if counterparty_lower not in haystack:
                            continue
                    result.append({
                        "date": str(voucher.date),
                        "org_nr": company.org_nr,
                        "company_name": company.name,
                        "account": tx.account,
                        "amount_kr": _ore_to_kr(tx.amount),
                        "voucher_text": voucher.text,
                        "tx_text": tx.text or "",
                    })
        result.sort(key=lambda r: (r["date"], r["org_nr"]))
        return result

    # ------------------------------------------------------------------
    # Jämförelser
    # ------------------------------------------------------------------

    def compare_accounts_across_companies(self, account: str) -> dict:
        """Jämför ett kontos saldo och aktivitet över alla bolag."""
        result = {}
        for company in self._companies:
            tx_list = [
                tx
                for v in company.vouchers
                for tx in v.transactions
                if tx.account == account
            ]
            if not tx_list and account not in company.opening_balances and account not in company.closing_balances:
                continue
            total_debit = sum(tx.amount for tx in tx_list if tx.amount >= 0)
            total_credit = sum(tx.amount for tx in tx_list if tx.amount < 0)
            result[company.org_nr] = {
                "name": company.name,
                "ib_kr": _ore_to_kr(int(company.opening_balances.get(account, 0) * 100)),
                "ub_kr": _ore_to_kr(int(company.closing_balances.get(account, 0) * 100)),
                "transaction_count": len(tx_list),
                "total_debit_kr": _ore_to_kr(total_debit),
                "total_credit_kr": _ore_to_kr(total_credit),
            }
        return result

    def trace_intercompany_flow(
        self,
        amount_kr: float,
        tolerance_kr: float = 500,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict]:
        """Spåra ett belopp genom sfären: hitta transaktioner med liknande belopp."""
        target_ore = int(amount_kr * 100)
        tol_ore = int(tolerance_kr * 100)
        result = []
        for company in self._companies:
            for voucher in company.vouchers:
                d = voucher.date
                if start_date and d < start_date:
                    continue
                if end_date and d > end_date:
                    continue
                for tx in voucher.transactions:
                    if abs(abs(tx.amount) - target_ore) <= tol_ore:
                        result.append({
                            "date": str(d),
                            "org_nr": company.org_nr,
                            "company_name": company.name,
                            "account": tx.account,
                            "amount_kr": _ore_to_kr(tx.amount),
                            "voucher_text": voucher.text,
                            "tx_text": tx.text or "",
                            "direction": "in" if tx.amount > 0 else "out",
                        })
        result.sort(key=lambda r: r["date"])
        return result
