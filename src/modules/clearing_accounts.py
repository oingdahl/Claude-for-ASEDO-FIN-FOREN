"""Modul C: Avräkningskonton.

Analyserar konton definierade i konfigurationen (clearing-konton,
ägarlånskonton etc.) och flaggar:
- old_open_balance   MEDIUM/HIGH  Öppet saldo >90 dagar utan rörelse
- owner_loan         MEDIUM       Transaktion på ägarlånskonto (2393)
- owner_loan         HIGH         Stor transaktion på ägarlånskonto (>tröskelvärde)
- growing_balance    MEDIUM       Saldo ökar månad för månad >3 månader i rad

Konfiguration (config['clearing_accounts']):
    accounts               : list[str]   Konton att bevaka
    owner_loan_accounts    : list[str]   Ägarlånskonton (default ["2393"])
    owner_loan_large_kr    : int         Gräns för HIGH (default 50 000 kr)
    old_balance_days       : int         Dagar utan rörelse (default 90)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from src.core.models import BankTransaction, Company, Voucher, Transaction, Finding
from src.modules.base_module import AnalysisModule

logger = logging.getLogger(__name__)

_DEFAULT_OWNER_LOAN_ACCOUNTS = ["2393"]
_DEFAULT_OWNER_LOAN_LARGE_KR = 50_000
_DEFAULT_OLD_BALANCE_DAYS = 90

# Standardintervall att kontrollera när inga konton är konfigurerade
_FALLBACK_CLEARING_ACCOUNTS = (
    [str(a) for a in range(1630, 1690)]
    + ["2393"]
    + [str(a) for a in range(2890, 2900)]
)


def _last_movement_date(company: Company, account_nr: str) -> date | None:
    """Returnerar senaste verifikationsdatum för ett konto, eller None."""
    latest: date | None = None
    for v in company.vouchers:
        for t in v.transactions:
            if t.account == account_nr:
                if latest is None or v.date > latest:
                    latest = v.date
    return latest


def _account_balance_from_vouchers(company: Company, account_nr: str) -> int:
    """Summerar alla transaktioner på kontot i öre."""
    return sum(
        t.amount
        for v in company.vouchers
        for t in v.transactions
        if t.account == account_nr
    )


def _monthly_abs_balances(company: Company, account_nr: str) -> list[tuple[tuple[int, int], Decimal]]:
    """Returnerar kumulativa absolutbelopp per (år, månad), äldst till nyast."""
    movements: dict[tuple[int, int], int] = defaultdict(int)
    for v in company.vouchers:
        for t in v.transactions:
            if t.account == account_nr:
                movements[(v.date.year, v.date.month)] += t.amount
    if not movements:
        return []
    sorted_months = sorted(movements)
    running = Decimal(0)
    result = []
    for ym in sorted_months:
        running += Decimal(movements[ym]) / 100
        result.append((ym, abs(running)))
    return result


class ClearingAccountsModule(AnalysisModule):
    """Modul C: Avräkningskonton."""

    @property
    def name(self) -> str:
        return "clearing_accounts"

    @property
    def description(self) -> str:
        return (
            "Analyserar clearing-konton och ägarlånskonton. Flaggar öppna "
            "saldon utan rörelse, ägarlånstransaktioner och växande saldon."
        )

    def analyze(
        self,
        companies: list[Company],
        bank_transactions: dict[str, list[BankTransaction]],
        config: dict,
    ) -> list[Finding]:
        ca_cfg = config.get("clearing_accounts", {})
        accounts_to_check = [str(a) for a in ca_cfg.get("accounts", [])]
        if not accounts_to_check:
            accounts_to_check = _FALLBACK_CLEARING_ACCOUNTS
        owner_loan_accounts = [
            str(a) for a in ca_cfg.get("owner_loan_accounts", _DEFAULT_OWNER_LOAN_ACCOUNTS)
        ]
        owner_loan_large_oren = (
            ca_cfg.get("owner_loan_large_kr", _DEFAULT_OWNER_LOAN_LARGE_KR) * 100
        )
        old_balance_days: int = ca_cfg.get("old_balance_days", _DEFAULT_OLD_BALANCE_DAYS)

        findings: list[Finding] = []
        for company in companies:
            findings.extend(
                self._analyze_company(
                    company,
                    accounts_to_check,
                    owner_loan_accounts,
                    owner_loan_large_oren,
                    old_balance_days,
                )
            )
        return findings

    # ------------------------------------------------------------------

    def _analyze_company(
        self,
        company: Company,
        accounts_to_check: list[str],
        owner_loan_accounts: list[str],
        owner_loan_large_oren: int,
        old_balance_days: int,
    ) -> list[Finding]:
        findings: list[Finding] = []

        # Referensdatum = senaste verifikationsdatum i bolaget
        if company.vouchers:
            ref_date = max(v.date for v in company.vouchers)
        else:
            return findings

        # Filtrera till konton som faktiskt förekommer i bolagets data
        all_tx_accounts = {
            t.account for v in company.vouchers for t in v.transactions
        }
        all_balance_accounts = set(company.closing_balances) | set(company.opening_balances)
        active_accounts = [
            a for a in accounts_to_check
            if a in all_tx_accounts or a in all_balance_accounts
        ]

        if not active_accounts:
            return [self._create_finding(
                category="no_clearing_accounts",
                risk_level="INFO",
                summary=(
                    f"{company.org_nr}: Inga clearing-konton hittades i data "
                    f"(kontrollerade {len(accounts_to_check)} konton)"
                ),
                companies=[company.org_nr],
                details={
                    "org_nr": company.org_nr,
                    "accounts_checked": len(accounts_to_check),
                },
            )]

        for account_nr in active_accounts:
            # -------------------------------------------------------
            # Ägarlån (2393)
            # -------------------------------------------------------
            if account_nr in owner_loan_accounts:
                for v in company.vouchers:
                    for t in v.transactions:
                        if t.account != account_nr:
                            continue
                        risk = (
                            "HIGH"
                            if abs(t.amount) >= owner_loan_large_oren
                            else "MEDIUM"
                        )
                        findings.append(self._create_finding(
                            category="owner_loan",
                            risk_level=risk,
                            summary=(
                                f"{company.org_nr}: Ägarlåntransaktion {t.amount / 100:.2f} kr "
                                f"på konto {account_nr} ({v.date})"
                            ),
                            companies=[company.org_nr],
                            details={
                                "org_nr": company.org_nr,
                                "account": account_nr,
                                "amount_oren": t.amount,
                                "amount_kr": t.amount / 100,
                                "date": str(v.date),
                                "voucher_text": v.text,
                            },
                        ))
                continue  # Ägarlån behandlas inte för old_open_balance

            # -------------------------------------------------------
            # Gammalt öppet saldo
            # -------------------------------------------------------
            last_move = _last_movement_date(company, account_nr)
            if last_move is None:
                continue  # Inga transaktioner — kontot berörs ej

            days_since = (ref_date - last_move).days
            balance = (
                company.closing_balances.get(account_nr)
                or Decimal(_account_balance_from_vouchers(company, account_nr)) / 100
            )

            if abs(balance) > Decimal("0.01") and days_since > old_balance_days:
                risk = "HIGH" if days_since > old_balance_days * 2 else "MEDIUM"
                findings.append(self._create_finding(
                    category="old_open_balance",
                    risk_level=risk,
                    summary=(
                        f"{company.org_nr}: Konto {account_nr} har öppet saldo "
                        f"{balance:.2f} kr utan rörelse i {days_since} dagar"
                    ),
                    companies=[company.org_nr],
                    details={
                        "org_nr": company.org_nr,
                        "account": account_nr,
                        "balance_kr": float(balance),
                        "last_movement_date": str(last_move),
                        "days_without_movement": days_since,
                        "threshold_days": old_balance_days,
                    },
                ))

            # -------------------------------------------------------
            # Växande saldo (>3 månader i rad)
            # -------------------------------------------------------
            monthly = _monthly_abs_balances(company, account_nr)
            if len(monthly) >= 4:
                abs_bals = [b for _, b in monthly]
                streak = 1
                for i in range(1, len(abs_bals)):
                    if abs_bals[i] > abs_bals[i - 1]:
                        streak += 1
                    else:
                        streak = 1
                    if streak > 3:
                        findings.append(self._create_finding(
                            category="growing_balance",
                            risk_level="MEDIUM",
                            summary=(
                                f"{company.org_nr}: Clearing-konto {account_nr} "
                                f"växer {streak} månader i rad"
                            ),
                            companies=[company.org_nr],
                            details={
                                "org_nr": company.org_nr,
                                "account": account_nr,
                                "months_growing": streak,
                            },
                        ))
                        break  # En finding per konto

        return findings
