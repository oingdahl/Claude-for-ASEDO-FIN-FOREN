"""Modul B: Koncernintern avstämning.

Analyslogik:
1. Per konfigurerade par (bolag A, konto A, bolag B, konto B) jämförs fordran
   mot skuld inom tolerans.
2. Konton utan konfigurerad motpart flaggas som ensidiga.
3. Cirkelflöden (A→B→C→A) detekteras via DFS i saldorelationsgrafen.
4. Kontobalanser som ökar >3 månader i rad (beräknat från verifikationer)
   flaggas som växande.

Konfiguration (config['intercompany']):
    tolerance_kr        : int   (default 500)
    pairs               : list  Explicit fordran/skuld-par, se nedan.
    receivables_range   : [int, int]  Fordranskonton (default [1660, 1669])
    payables_range      : [int, int]  Skuldkonton   (default [2460, 2469])
    known_companies     : list[str]   Org_nr för kända bolag i sfären.

Pair-format:
    {"company_a": "<org_nr>", "account_a": "<kontonr>",
     "company_b": "<org_nr>", "account_b": "<kontonr>"}
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal

from src.core.models import BankTransaction, Company, Finding
from src.modules.base_module import AnalysisModule

logger = logging.getLogger(__name__)

_DEFAULT_TOLERANCE_KR = 500
_DEFAULT_REC_RANGE = (1660, 1669)
_DEFAULT_PAY_RANGE = (2460, 2469)


def _in_range(account_nr: str, lo: int, hi: int) -> bool:
    try:
        return lo <= int(account_nr) <= hi
    except ValueError:
        return False


def _monthly_balances(company: Company, account_nr: str) -> dict[tuple[int, int], Decimal]:
    """Beräknar saldo per (år, månad) för ett konto utifrån verifikationer.

    Kumulativt saldo: adderar beloppen från äldst till nyast.
    """
    movements: dict[tuple[int, int], int] = defaultdict(int)
    for v in company.vouchers:
        for t in v.transactions:
            if t.account == account_nr:
                key = (v.date.year, v.date.month)
                movements[key] += t.amount

    if not movements:
        return {}

    # Sortera månader och bygg kumulativt saldo
    sorted_months = sorted(movements)
    running = Decimal(0)
    result: dict[tuple[int, int], Decimal] = {}
    for ym in sorted_months:
        running += Decimal(movements[ym]) / 100
        result[ym] = running
    return result


def _detect_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Hitta alla enkla cykler av längd ≥ 3 med DFS (Johnson-inspirerat, förenklat)."""
    all_nodes = list(graph)
    cycles: list[list[str]] = []
    visited_starts: set[str] = set()

    def dfs(start: str, current: str, path: list[str]) -> None:
        for neighbour in graph.get(current, []):
            if neighbour == start and len(path) >= 3:
                cycles.append(list(path))
            elif neighbour not in visited_starts and neighbour not in path:
                path.append(neighbour)
                dfs(start, neighbour, path)
                path.pop()

    for node in all_nodes:
        dfs(node, node, [node])
        visited_starts.add(node)

    return cycles


class IntercompanyModule(AnalysisModule):
    """Modul B: Koncernintern avstämning."""

    @property
    def name(self) -> str:
        return "intercompany"

    @property
    def description(self) -> str:
        return (
            "Stämmer av koncerninterna fordringar och skulder mellan bolag i sfären. "
            "Identifierar obalanser, ensidiga poster, cirkelflöden och växande saldon."
        )

    def analyze(
        self,
        companies: list[Company],
        bank_transactions: dict[str, list[BankTransaction]],
        config: dict,
    ) -> list[Finding]:
        """Kör koncernintern avstämning.

        Kräver att ``config['intercompany']['pairs']`` är satt för meningsfull
        analys. Utan par körs enbart automatisk sökning på kontointervall.
        """
        ic_cfg = config.get("intercompany", {})
        tolerance = Decimal(str(ic_cfg.get("tolerance_kr", _DEFAULT_TOLERANCE_KR)))
        rec_lo, rec_hi = ic_cfg.get("receivables_range", list(_DEFAULT_REC_RANGE))
        pay_lo, pay_hi = ic_cfg.get("payables_range", list(_DEFAULT_PAY_RANGE))
        pairs: list[dict] = ic_cfg.get("pairs", [])
        known_companies: set[str] = set(
            ic_cfg.get("known_companies", [c.org_nr for c in companies])
        )

        company_map: dict[str, Company] = {c.org_nr: c for c in companies}
        findings: list[Finding] = []

        # ---------------------------------------------------------------
        # 1. Parvisa obalanser och ensidiga poster
        # ---------------------------------------------------------------
        checked_accounts: set[tuple[str, str]] = set()  # (org_nr, account_nr)

        for pair in pairs:
            org_a = pair["company_a"]
            acc_a = str(pair["account_a"])
            org_b = pair["company_b"]
            acc_b = str(pair["account_b"])

            comp_a = company_map.get(org_a)
            comp_b = company_map.get(org_b)

            checked_accounts.add((org_a, acc_a))
            checked_accounts.add((org_b, acc_b))

            if comp_a is None or comp_b is None:
                missing = org_a if comp_a is None else org_b
                findings.append(self._create_finding(
                    category="ic_one_sided",
                    risk_level="HIGH",
                    summary=f"Motpart {missing} saknas i sfären för konfigurerat par",
                    companies=[org_a, org_b],
                    details={"pair": pair},
                ))
                continue

            a_has_account = acc_a in comp_a.closing_balances
            b_has_account = acc_b in comp_b.closing_balances
            bal_a = comp_a.closing_balances.get(acc_a, Decimal(0))
            bal_b = comp_b.closing_balances.get(acc_b, Decimal(0))

            # Ensidig: ett konto existerar, motpartens konto saknas helt
            if (a_has_account and abs(bal_a) > Decimal("0.01")) and not b_has_account:
                findings.append(self._create_finding(
                    category="ic_one_sided",
                    risk_level="HIGH",
                    summary=(
                        f"{org_a}: Fordran {bal_a:.2f} kr på konto {acc_a} "
                        f"saknar motpost hos {org_b} (konto {acc_b} saknas)"
                    ),
                    companies=[org_a, org_b],
                    details={
                        "org_nr_a": org_a, "account_a": acc_a,
                        "balance_a_kr": float(bal_a),
                        "org_nr_b": org_b, "account_b": acc_b,
                    },
                ))
                continue

            if (b_has_account and abs(bal_b) > Decimal("0.01")) and not a_has_account:
                findings.append(self._create_finding(
                    category="ic_one_sided",
                    risk_level="HIGH",
                    summary=(
                        f"{org_b}: Skuld {bal_b:.2f} kr på konto {acc_b} "
                        f"saknar motpost hos {org_a} (konto {acc_a} saknas)"
                    ),
                    companies=[org_a, org_b],
                    details={
                        "org_nr_a": org_a, "account_a": acc_a,
                        "org_nr_b": org_b, "account_b": acc_b,
                        "balance_b_kr": float(bal_b),
                    },
                ))
                continue

            # Fordran (positiv) + skuld (negativ) bör summera till ~0
            diff = abs(bal_a + bal_b)
            if diff > tolerance:
                findings.append(self._create_finding(
                    category="ic_imbalance",
                    risk_level="HIGH",
                    summary=(
                        f"Obalans {diff:.2f} kr: {org_a} fordran {bal_a:.2f} kr "
                        f"(konto {acc_a}) vs {org_b} skuld {bal_b:.2f} kr "
                        f"(konto {acc_b})"
                    ),
                    companies=[org_a, org_b],
                    details={
                        "org_nr_a": org_a, "account_a": acc_a,
                        "balance_a_kr": float(bal_a),
                        "org_nr_b": org_b, "account_b": acc_b,
                        "balance_b_kr": float(bal_b),
                        "diff_kr": float(diff),
                        "tolerance_kr": float(tolerance),
                    },
                ))

        # ---------------------------------------------------------------
        # 2. Automatisk sökning: IC-konton utan konfigurerat par
        # ---------------------------------------------------------------
        for company in companies:
            for acc_nr, balance in company.closing_balances.items():
                if (company.org_nr, acc_nr) in checked_accounts:
                    continue
                is_rec = _in_range(acc_nr, rec_lo, rec_hi)
                is_pay = _in_range(acc_nr, pay_lo, pay_hi)
                if not (is_rec or is_pay):
                    continue
                if abs(balance) < Decimal("0.01"):
                    continue
                findings.append(self._create_finding(
                    category="ic_one_sided",
                    risk_level="HIGH",
                    summary=(
                        f"{company.org_nr}: IC-konto {acc_nr} har saldo {balance:.2f} kr "
                        f"utan konfigurerad motpart"
                    ),
                    companies=[company.org_nr],
                    details={
                        "org_nr": company.org_nr,
                        "account": acc_nr,
                        "balance_kr": float(balance),
                    },
                ))

        # ---------------------------------------------------------------
        # 3. Cirkelflöden — bygg graf och detektera cykler
        # ---------------------------------------------------------------
        # Kant A→B: A har fordran på B (positiv saldo på fordranskonto)
        graph: dict[str, list[str]] = defaultdict(list)
        for pair in pairs:
            org_a = pair["company_a"]
            org_b = pair["company_b"]
            acc_a = str(pair["account_a"])
            comp_a = company_map.get(org_a)
            if comp_a is None:
                continue
            bal_a = comp_a.closing_balances.get(acc_a, Decimal(0))
            if bal_a > Decimal("0.01"):
                graph[org_a].append(org_b)

        cycles = _detect_cycles(dict(graph))
        for cycle in cycles:
            org_list = cycle
            findings.append(self._create_finding(
                category="ic_circular",
                risk_level="HIGH",
                summary=f"Cirkelflöde detekterat: {'→'.join(org_list + [org_list[0]])}",
                companies=org_list,
                details={"cycle": org_list},
            ))

        # ---------------------------------------------------------------
        # 4. Växande saldo — beräkna månadsbalanser per IC-konto
        # ---------------------------------------------------------------
        for pair in pairs:
            for org_nr, acc_nr in [(pair["company_a"], pair["account_a"]),
                                   (pair["company_b"], pair["account_b"])]:
                company = company_map.get(org_nr)
                if company is None:
                    continue
                monthly = _monthly_balances(company, str(acc_nr))
                if len(monthly) < 4:
                    continue
                sorted_months = sorted(monthly)
                # Hitta sekvens av >3 månader med ökande absolutbelopp
                abs_bals = [abs(monthly[m]) for m in sorted_months]
                streak = 1
                max_streak = 1
                for i in range(1, len(abs_bals)):
                    if abs_bals[i] > abs_bals[i - 1]:
                        streak += 1
                        max_streak = max(max_streak, streak)
                    else:
                        streak = 1
                if max_streak > 3:
                    findings.append(self._create_finding(
                        category="ic_growing_balance",
                        risk_level="MEDIUM",
                        summary=(
                            f"{org_nr}: IC-konto {acc_nr} växer i saldo "
                            f"under {max_streak} månader i rad"
                        ),
                        companies=[org_nr],
                        details={
                            "org_nr": org_nr,
                            "account": str(acc_nr),
                            "months_growing": max_streak,
                            "monthly_balances": {
                                f"{ym[0]}-{ym[1]:02d}": float(monthly[ym])
                                for ym in sorted_months
                            },
                        },
                    ))

        logger.info("%d IC-fynd genererade", len(findings))
        return findings
