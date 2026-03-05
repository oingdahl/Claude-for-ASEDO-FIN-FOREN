"""Modul F: Löner och kreditkort.

Analyser:
- salary_deviation    HIGH    Lön >2 stddev från medelvärde (per konto)
- duplicate_salary    HIGH    Samma lönbelopp >1 gång samma månad
- credit_card_expense MEDIUM  Kreditkortspost >20 000 kr

Konfiguration (config['salary_expenses']):
    salary_accounts          : list[str]  Lönekonton (default ["7010", "7210", "7710"])
    salary_deviation_factor  : float      Stddev-faktor (default 2.0)
    salary_min_months        : int        Minsta månader för stddev-test (default 3)
    credit_card_accounts     : list[str]  Kreditkortskonton (default 6540-6570)
    credit_card_keywords     : list[str]  Nyckelord i text (default ["Visa","Mastercard","kreditkort"])
    credit_card_min_kr       : int        Minsta belopp i kr (default 20 000)
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import date

from src.core.models import BankTransaction, Company, Finding, Transaction, Voucher
from src.modules.base_module import AnalysisModule

logger = logging.getLogger(__name__)

_DEFAULT_SALARY_ACCOUNTS = ["7010", "7210", "7710"]
_DEFAULT_DEV_FACTOR = 2.0
_DEFAULT_MIN_MONTHS = 3
_DEFAULT_CC_ACCOUNTS_RANGE = (6540, 6570)
_DEFAULT_CC_KEYWORDS = ["Visa", "Mastercard", "kreditkort"]
_DEFAULT_CC_MIN_KR = 20_000


def _is_credit_card(account_nr: str, text: str, cc_accounts: list[str], keywords: list[str]) -> bool:
    if account_nr in cc_accounts:
        return True
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


class SalaryExpensesModule(AnalysisModule):
    """Modul F: Löner och kreditkort."""

    @property
    def name(self) -> str:
        return "salary_expenses"

    @property
    def description(self) -> str:
        return (
            "Analyserar löneutbetalningar (avvikelser, dubbletter) och "
            "kreditkortsutgifter (stora belopp)."
        )

    def analyze(
        self,
        companies: list[Company],
        bank_transactions: dict[str, list[BankTransaction]],
        config: dict,
    ) -> list[Finding]:
        se_cfg = config.get("salary_expenses", {})
        salary_accounts: list[str] = [
            str(a) for a in se_cfg.get("salary_accounts", _DEFAULT_SALARY_ACCOUNTS)
        ]
        dev_factor: float = se_cfg.get("salary_deviation_factor", _DEFAULT_DEV_FACTOR)
        min_months: int = se_cfg.get("salary_min_months", _DEFAULT_MIN_MONTHS)
        cc_range = _DEFAULT_CC_ACCOUNTS_RANGE
        cc_accounts: list[str] = [
            str(a) for a in se_cfg.get("credit_card_accounts",
            list(range(cc_range[0], cc_range[1] + 1)))
        ]
        cc_keywords: list[str] = se_cfg.get("credit_card_keywords", _DEFAULT_CC_KEYWORDS)
        cc_min_oren: int = se_cfg.get("credit_card_min_kr", _DEFAULT_CC_MIN_KR) * 100

        findings: list[Finding] = []
        for company in companies:
            findings.extend(
                self._analyze_salaries(company, salary_accounts, dev_factor, min_months)
            )
            findings.extend(
                self._analyze_credit_cards(company, cc_accounts, cc_keywords, cc_min_oren)
            )
        return findings

    # ------------------------------------------------------------------

    def _analyze_salaries(
        self,
        company: Company,
        salary_accounts: list[str],
        dev_factor: float,
        min_months: int,
    ) -> list[Finding]:
        findings: list[Finding] = []

        # Gruppa per konto → lista av (year, month, amount, voucher)
        by_account: dict[str, list[tuple[int, int, int, Voucher]]] = defaultdict(list)
        for v in company.vouchers:
            for t in v.transactions:
                if t.account in salary_accounts:
                    by_account[t.account].append((v.date.year, v.date.month, t.amount, v))

        for account_nr, entries in by_account.items():
            amounts = [e[2] for e in entries]

            # Avvikande löner — behöver minst min_months + 1 datapunkter
            if len(amounts) >= min_months + 1:
                try:
                    mean = statistics.mean(amounts)
                    stdev = statistics.stdev(amounts)
                except statistics.StatisticsError:
                    stdev = 0.0

                if stdev > 0:
                    for yr, mo, amt, v in entries:
                        if abs(amt - mean) > dev_factor * stdev:
                            findings.append(self._create_finding(
                                category="salary_deviation",
                                risk_level="HIGH",
                                summary=(
                                    f"{company.org_nr}: Avvikande lön {amt / 100:.0f} kr "
                                    f"på konto {account_nr} ({yr}-{mo:02d}) — "
                                    f"medel {mean / 100:.0f} kr ±{stdev / 100:.0f} kr"
                                ),
                                companies=[company.org_nr],
                                details={
                                    "org_nr": company.org_nr,
                                    "account": account_nr,
                                    "amount_kr": amt / 100,
                                    "mean_kr": mean / 100,
                                    "stdev_kr": stdev / 100,
                                    "deviation_factor": abs(amt - mean) / stdev,
                                    "year": yr,
                                    "month": mo,
                                    "voucher_text": v.text,
                                },
                            ))

            # Dubbelbetalning — samma belopp >1 gång samma månad
            by_month: dict[tuple[int, int], list[int]] = defaultdict(list)
            for yr, mo, amt, _ in entries:
                by_month[(yr, mo)].append(amt)

            for (yr, mo), month_amounts in by_month.items():
                seen: dict[int, int] = defaultdict(int)
                for amt in month_amounts:
                    seen[amt] += 1
                for amt, count in seen.items():
                    if count > 1:
                        findings.append(self._create_finding(
                            category="duplicate_salary",
                            risk_level="HIGH",
                            summary=(
                                f"{company.org_nr}: Lön {amt / 100:.0f} kr "
                                f"förekommer {count} gånger i {yr}-{mo:02d} "
                                f"på konto {account_nr}"
                            ),
                            companies=[company.org_nr],
                            details={
                                "org_nr": company.org_nr,
                                "account": account_nr,
                                "amount_kr": amt / 100,
                                "count": count,
                                "year": yr,
                                "month": mo,
                            },
                        ))

        return findings

    def _analyze_credit_cards(
        self,
        company: Company,
        cc_accounts: list[str],
        keywords: list[str],
        min_oren: int,
    ) -> list[Finding]:
        findings: list[Finding] = []
        for v in company.vouchers:
            for t in v.transactions:
                if abs(t.amount) < min_oren:
                    continue
                if not _is_credit_card(t.account, t.text or "", cc_accounts, keywords):
                    continue
                findings.append(self._create_finding(
                    category="credit_card_expense",
                    risk_level="MEDIUM",
                    summary=(
                        f"{company.org_nr}: Stor kreditkortspost {abs(t.amount) / 100:.0f} kr "
                        f"på konto {t.account} ({v.date})"
                    ),
                    companies=[company.org_nr],
                    details={
                        "org_nr": company.org_nr,
                        "account": t.account,
                        "amount_kr": abs(t.amount) / 100,
                        "date": str(v.date),
                        "text": t.text or "",
                        "voucher_text": v.text,
                    },
                ))
        return findings
