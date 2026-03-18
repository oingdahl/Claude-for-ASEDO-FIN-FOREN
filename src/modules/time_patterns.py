"""Modul E: Tidsmönster.

Analyser:
- weekend_transaction  MEDIUM  Verifikation på lördag/söndag eller helgdag
- closing_cluster      HIGH    Transaktioner i slutet av period >2x normal frekvens
- reversal             HIGH    Transaktion nära bokslut med exakt omvänd post
- activity_gap         MEDIUM  >14 dagars gap i bokföring (exkl. juli)

Konfiguration (config['time_patterns']):
    closing_window_days   : int    Dagar i bokslutskluster (default 5)
    closing_ratio         : float  Gångerfaktor (default 2.0)
    reversal_window_days  : int    Dagar för att söka reversering (default 30)
    gap_threshold_days    : int    Dagar för lucka (default 14)
    cost_accounts_start   : int    Start kostnadskonton (default 4000)
    cost_accounts_end     : int    Slut kostnadskonton (default 7999)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta

from src.core.models import BankTransaction, Company, Finding, FiscalYear, Transaction, Voucher
from src.core.utils import swedish_holidays
from src.modules.base_module import AnalysisModule

logger = logging.getLogger(__name__)

_DEFAULT_CLOSING_WINDOW = 5
_DEFAULT_CLOSING_RATIO = 2.0
_DEFAULT_REVERSAL_WINDOW = 30
_DEFAULT_GAP_THRESHOLD = 14


def _get_period_end(company: Company) -> date | None:
    """Returnerar sista dag i räkenskapsåret (index 0) eller sista verifikationsdatum."""
    if company.fiscal_years:
        fy = min(company.fiscal_years, key=lambda y: abs(y.year_index))
        return fy.end_date
    if company.vouchers:
        return max(v.date for v in company.vouchers)
    return None


def _get_period_start(company: Company) -> date | None:
    if company.fiscal_years:
        fy = min(company.fiscal_years, key=lambda y: abs(y.year_index))
        return fy.start_date
    if company.vouchers:
        return min(v.date for v in company.vouchers)
    return None


class TimePatternsModule(AnalysisModule):
    """Modul E: Tidsmönster."""

    @property
    def name(self) -> str:
        return "time_patterns"

    @property
    def description(self) -> str:
        return (
            "Analyserar tidsmönster i bokföring: helgtransaktioner, "
            "bokslutskluster, reverseringar och aktivitetsluckor."
        )

    def analyze(
        self,
        companies: list[Company],
        bank_transactions: dict[str, list[BankTransaction]],
        config: dict,
    ) -> list[Finding]:
        tp_cfg = config.get("time_patterns", {})
        closing_window: int = tp_cfg.get("closing_window_days", _DEFAULT_CLOSING_WINDOW)
        closing_ratio: float = tp_cfg.get("closing_ratio", _DEFAULT_CLOSING_RATIO)
        reversal_window: int = tp_cfg.get("reversal_window_days", _DEFAULT_REVERSAL_WINDOW)
        gap_threshold: int = tp_cfg.get("gap_threshold_days", _DEFAULT_GAP_THRESHOLD)

        findings: list[Finding] = []
        for company in companies:
            period_end = _get_period_end(company)
            if period_end is None:
                continue

            findings.extend(self._check_weekend_transactions(company))
            findings.extend(
                self._check_closing_cluster(company, period_end, closing_window, closing_ratio)
            )
            findings.extend(
                self._check_reversals(company, period_end, reversal_window)
            )
            findings.extend(
                self._check_activity_gaps(company, gap_threshold)
            )
        return findings

    # ------------------------------------------------------------------

    def _check_weekend_transactions(self, company: Company) -> list[Finding]:
        findings: list[Finding] = []
        holiday_cache: dict[int, set[date]] = {}

        for v in company.vouchers:
            d = v.date
            year = d.year
            if year not in holiday_cache:
                holiday_cache[year] = swedish_holidays(year)

            is_weekend = d.weekday() >= 5  # lördag=5, söndag=6
            is_holiday = d in holiday_cache[year]

            if is_weekend or is_holiday:
                reason = "helgdag" if is_holiday else ("lördag" if d.weekday() == 5 else "söndag")
                findings.append(self._create_finding(
                    category="weekend_transaction",
                    risk_level="MEDIUM",
                    summary=(
                        f"{company.org_nr}: Verifikation {v.series}{v.number} "
                        f"daterad {d} ({reason})"
                    ),
                    companies=[company.org_nr],
                    details={
                        "org_nr": company.org_nr,
                        "voucher": f"{v.series}{v.number}",
                        "date": str(d),
                        "reason": reason,
                        "voucher_text": v.text,
                    },
                ))
        return findings

    def _check_closing_cluster(
        self,
        company: Company,
        period_end: date,
        window_days: int,
        ratio: float,
    ) -> list[Finding]:
        window_start = period_end - timedelta(days=window_days - 1)

        in_window: list[Voucher] = []
        out_window: list[Voucher] = []
        for v in company.vouchers:
            if window_start <= v.date <= period_end:
                in_window.append(v)
            elif v.date < window_start:
                out_window.append(v)

        if not out_window or not in_window:
            return []

        period_start = _get_period_start(company) or min(v.date for v in company.vouchers)
        days_outside = (window_start - period_start).days
        if days_outside <= 0:
            return []

        daily_avg_outside = len(out_window) / days_outside
        daily_rate_window = len(in_window) / window_days

        if daily_avg_outside > 0 and daily_rate_window > ratio * daily_avg_outside:
            return [self._create_finding(
                category="closing_cluster",
                risk_level="HIGH",
                summary=(
                    f"{company.org_nr}: Bokslutskluster — {len(in_window)} verifikationer "
                    f"de sista {window_days} dagarna "
                    f"({daily_rate_window:.1f}/dag vs normalt {daily_avg_outside:.1f}/dag)"
                ),
                companies=[company.org_nr],
                details={
                    "org_nr": company.org_nr,
                    "period_end": str(period_end),
                    "window_days": window_days,
                    "count_in_window": len(in_window),
                    "daily_rate_window": round(daily_rate_window, 2),
                    "daily_avg_outside": round(daily_avg_outside, 4),
                    "ratio": round(daily_rate_window / daily_avg_outside, 2),
                },
            )]
        return []

    def _check_reversals(
        self,
        company: Company,
        period_end: date,
        window_days: int,
    ) -> list[Finding]:
        """Hitta transaktioner nära bokslut som reverseras kort därefter."""
        near_end_start = period_end - timedelta(days=window_days)

        # Indexera transaktioner per (konto, belopp) → list av (datum, verifikation)
        tx_index: dict[tuple[str, int], list[tuple[date, Voucher, Transaction]]] = defaultdict(list)
        for v in company.vouchers:
            for t in v.transactions:
                tx_index[(t.account, t.amount)].append((v.date, v, t))

        findings: list[Finding] = []
        seen_pairs: set[tuple[int, int]] = set()  # (id(v1), id(v2)) för deduplicering

        for v in company.vouchers:
            if not (near_end_start <= v.date <= period_end):
                continue
            for t in v.transactions:
                # Sök omvänd transaktion: same account, opposite amount
                reverse_key = (t.account, -t.amount)
                for rev_date, rev_v, rev_t in tx_index.get(reverse_key, []):
                    if rev_date < v.date:
                        continue
                    if (rev_date - v.date).days > window_days:
                        continue
                    pair_key = (id(v), id(rev_v))
                    if pair_key in seen_pairs or id(v) == id(rev_v):
                        continue
                    seen_pairs.add(pair_key)
                    seen_pairs.add((id(rev_v), id(v)))

                    findings.append(self._create_finding(
                        category="reversal",
                        risk_level="HIGH",
                        summary=(
                            f"{company.org_nr}: Möjlig reversering — "
                            f"verifikation {v.date} ({t.amount / 100:.0f} kr) "
                            f"reverseras {rev_date} ({rev_t.amount / 100:.0f} kr) "
                            f"nära bokslut {period_end}"
                        ),
                        companies=[company.org_nr],
                        details={
                            "org_nr": company.org_nr,
                            "account": t.account,
                            "original_date": str(v.date),
                            "original_amount_kr": t.amount / 100,
                            "reversal_date": str(rev_date),
                            "reversal_amount_kr": rev_t.amount / 100,
                            "period_end": str(period_end),
                            "days_between": (rev_date - v.date).days,
                        },
                    ))
        return findings

    def _check_activity_gaps(
        self,
        company: Company,
        gap_threshold: int,
    ) -> list[Finding]:
        """Hittar perioder >gap_threshold dagar utan bokföring (exkl. juli)."""
        if not company.vouchers:
            return []

        dates = sorted({v.date for v in company.vouchers})
        findings: list[Finding] = []

        for i in range(len(dates) - 1):
            d1 = dates[i]
            d2 = dates[i + 1]
            gap = (d2 - d1).days
            if gap <= gap_threshold:
                continue
            # Exkludera gaps som startar i juli
            if d1.month == 7:
                continue
            findings.append(self._create_finding(
                category="activity_gap",
                risk_level="MEDIUM",
                summary=(
                    f"{company.org_nr}: {gap} dagars gap i bokföring "
                    f"({d1} → {d2})"
                ),
                companies=[company.org_nr],
                details={
                    "org_nr": company.org_nr,
                    "gap_start": str(d1),
                    "gap_end": str(d2),
                    "gap_days": gap,
                    "threshold_days": gap_threshold,
                },
            ))
        return findings
