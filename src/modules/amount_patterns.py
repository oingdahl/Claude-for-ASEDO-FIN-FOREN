"""Modul D: Beloppsmönster och Benfords lag.

Analyser som körs:
- benford_deviation   HIGH    Chi-kvadrat-test mot Benfords lag (p < signifikansnivå)
- below_threshold     MEDIUM  Kluster av belopp strax under konfig-trösklar (>3 st)
- repeated_amount     MEDIUM  Samma belopp+text förekommer >N gånger
- round_amounts       LOW     >25% jämna belopp (delbart med 1 000 kr) per konto

Konfiguration (config['amount_patterns']):
    benford_min_count       : int    Minsta antal transaktioner för Benford (default 50)
    benford_significance    : float  Signifikansnivå för chi-kvadrat (default 0.05)
    benford_min_amount_kr   : int    Minsta |belopp| för Benford (default 10 kr)
    thresholds_kr           : list   Gränsvärden i kr (default [10000,20000,25000,50000,100000])
    below_threshold_margin  : float  % under tröskel (default 0.05 = 5 %)
    below_threshold_min_count : int  Min antal träffar (default 3)
    repeated_amount_min     : int    Min frekvens för upprepade belopp (default 5)
    round_amount_ratio      : float  Andel jämna belopp per konto (default 0.25)
    round_amount_divisor_kr : int    Divisor för "jämnt" (default 1000 kr)
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Sequence

from scipy.stats import chisquare

from src.core.models import BankTransaction, Company, Finding
from src.modules.base_module import AnalysisModule

logger = logging.getLogger(__name__)

# Benfords förväntade sannolikheter för första siffran 1–9
_BENFORD_PROBS: list[float] = [math.log10(1 + 1 / d) for d in range(1, 10)]

_DEFAULT_BENFORD_MIN = 50
_DEFAULT_BENFORD_SIG = 0.05
_DEFAULT_BENFORD_MIN_KR = 10
_DEFAULT_THRESHOLDS_KR = [10_000, 20_000, 25_000, 50_000, 100_000]
_DEFAULT_BELOW_MARGIN = 0.05
_DEFAULT_BELOW_MIN = 3
_DEFAULT_REPEATED_MIN = 5
_DEFAULT_ROUND_RATIO = 0.25
_DEFAULT_ROUND_DIVISOR_KR = 1_000


def _first_digit(amount_oren: int) -> int | None:
    """Returnerar första siffran (1–9) i absolutbeloppet, eller None om 0."""
    abs_val = abs(amount_oren)
    if abs_val == 0:
        return None
    s = str(abs_val).lstrip("0")
    return int(s[0]) if s else None


def _extract_first_digits(amounts: list[int], min_oren: int) -> list[int]:
    """Extraherar första siffror från belopp över min_oren."""
    result = []
    for a in amounts:
        if abs(a) < min_oren:
            continue
        d = _first_digit(a)
        if d is not None:
            result.append(d)
    return result


def _benford_chi2(digits: list[int]) -> tuple[float, float]:
    """Returnerar (chi2_statistic, p_value) för en lista av första siffror."""
    n = len(digits)
    observed = [digits.count(d) for d in range(1, 10)]
    expected = [p * n for p in _BENFORD_PROBS]
    stat, p = chisquare(observed, f_exp=expected)
    return float(stat), float(p)


class AmountPatternsModule(AnalysisModule):
    """Modul D: Beloppsmönster och Benfords lag."""

    @property
    def name(self) -> str:
        return "amount_patterns"

    @property
    def description(self) -> str:
        return (
            "Analyserar beloppsmönster: Benfords lag (chi-kvadrat), "
            "belopp strax under gränsvärden, upprepade belopp och jämna belopp."
        )

    def analyze(
        self,
        companies: list[Company],
        bank_transactions: dict[str, list[BankTransaction]],
        config: dict,
    ) -> list[Finding]:
        ap_cfg = config.get("amount_patterns", {})
        benford_min: int = ap_cfg.get("benford_min_count", _DEFAULT_BENFORD_MIN)
        benford_sig: float = ap_cfg.get("benford_significance", _DEFAULT_BENFORD_SIG)
        benford_min_oren: int = ap_cfg.get("benford_min_amount_kr", _DEFAULT_BENFORD_MIN_KR) * 100
        thresholds_oren: list[int] = [
            t * 100 for t in ap_cfg.get("thresholds_kr", _DEFAULT_THRESHOLDS_KR)
        ]
        below_margin: float = ap_cfg.get("below_threshold_margin", _DEFAULT_BELOW_MARGIN)
        below_min: int = ap_cfg.get("below_threshold_min_count", _DEFAULT_BELOW_MIN)
        repeated_min: int = ap_cfg.get("repeated_amount_min", _DEFAULT_REPEATED_MIN)
        round_ratio: float = ap_cfg.get("round_amount_ratio", _DEFAULT_ROUND_RATIO)
        round_div_oren: int = ap_cfg.get("round_amount_divisor_kr", _DEFAULT_ROUND_DIVISOR_KR) * 100

        findings: list[Finding] = []

        for company in companies:
            all_amounts = [
                t.amount
                for v in company.vouchers
                for t in v.transactions
            ]
            findings.extend(
                self._analyze_benford(
                    company, all_amounts, benford_min, benford_sig, benford_min_oren
                )
            )
            findings.extend(
                self._analyze_below_threshold(
                    company, all_amounts, thresholds_oren, below_margin, below_min
                )
            )
            findings.extend(
                self._analyze_repeated_amounts(company, benford_min_oren, repeated_min)
            )
            findings.extend(
                self._analyze_round_amounts(company, round_div_oren, round_ratio)
            )

        return findings

    # ------------------------------------------------------------------

    def _analyze_benford(
        self,
        company: Company,
        amounts: list[int],
        min_count: int,
        significance: float,
        min_oren: int,
    ) -> list[Finding]:
        digits = _extract_first_digits(amounts, min_oren)
        if len(digits) < min_count:
            logger.debug(
                "%s: Hoppar över Benford-test (%d poster < %d)",
                company.org_nr, len(digits), min_count,
            )
            return [self._create_finding(
                category="benford_skipped",
                risk_level="INFO",
                summary=(
                    f"{company.org_nr}: Benford-test ej utfört — "
                    f"för få transaktioner ({len(digits)} < {min_count})"
                ),
                companies=[company.org_nr],
                details={
                    "org_nr": company.org_nr,
                    "sample_size": len(digits),
                    "required_min": min_count,
                },
            )]

        chi2, p = _benford_chi2(digits)
        logger.debug(
            "%s: Benford chi2=%.3f p=%.4f (n=%d)", company.org_nr, chi2, p, len(digits)
        )

        if p < significance:
            return [self._create_finding(
                category="benford_deviation",
                risk_level="HIGH",
                summary=(
                    f"{company.org_nr}: Beloppsmönster avviker från Benfords lag "
                    f"(χ²={chi2:.2f}, p={p:.4f}, n={len(digits)})"
                ),
                companies=[company.org_nr],
                details={
                    "org_nr": company.org_nr,
                    "chi2_statistic": chi2,
                    "p_value": p,
                    "significance_level": significance,
                    "n": len(digits),
                    "observed": [digits.count(d) for d in range(1, 10)],
                    "expected": [round(p_ * len(digits), 2) for p_ in _BENFORD_PROBS],
                },
            )]
        return []

    def _analyze_below_threshold(
        self,
        company: Company,
        amounts: list[int],
        thresholds_oren: list[int],
        margin: float,
        min_count: int,
    ) -> list[Finding]:
        findings: list[Finding] = []
        for threshold in thresholds_oren:
            lower = int(threshold * (1 - margin))
            # Belopp i intervallet [lower, threshold)
            hits = [a for a in amounts if lower <= abs(a) < threshold]
            if len(hits) > min_count:
                threshold_kr = threshold // 100
                findings.append(self._create_finding(
                    category="below_threshold",
                    risk_level="MEDIUM",
                    summary=(
                        f"{company.org_nr}: {len(hits)} transaktioner strax under "
                        f"{threshold_kr:,} kr-gränsen "
                        f"({lower // 100:,}–{threshold_kr - 1:,} kr)"
                    ),
                    companies=[company.org_nr],
                    details={
                        "org_nr": company.org_nr,
                        "threshold_kr": threshold_kr,
                        "lower_bound_kr": lower // 100,
                        "count": len(hits),
                        "min_count": min_count,
                        "amounts_kr": sorted([abs(a) / 100 for a in hits]),
                    },
                ))
        return findings

    def _analyze_repeated_amounts(
        self,
        company: Company,
        min_oren: int,
        min_count: int,
    ) -> list[Finding]:
        """Flaggar belopp+text-kombinationer som upprepas mer än min_count gånger."""
        freq: dict[tuple[int, str], list[str]] = defaultdict(list)
        for v in company.vouchers:
            for t in v.transactions:
                if abs(t.amount) < min_oren:
                    continue
                key = (t.amount, (t.text or "").strip())
                freq[key].append(str(v.date))

        findings: list[Finding] = []
        for (amount, text), dates in freq.items():
            if len(dates) > min_count:
                findings.append(self._create_finding(
                    category="repeated_amount",
                    risk_level="MEDIUM",
                    summary=(
                        f"{company.org_nr}: Belopp {amount / 100:.2f} kr "
                        f"med text '{text}' upprepas {len(dates)} gånger"
                    ),
                    companies=[company.org_nr],
                    details={
                        "org_nr": company.org_nr,
                        "amount_oren": amount,
                        "amount_kr": amount / 100,
                        "text": text,
                        "count": len(dates),
                        "dates": sorted(dates),
                    },
                ))
        return findings

    def _analyze_round_amounts(
        self,
        company: Company,
        divisor_oren: int,
        min_ratio: float,
    ) -> list[Finding]:
        """Flaggar konton där >min_ratio av transaktionerna är jämna belopp."""
        # Gruppa per konto
        by_account: dict[str, list[int]] = defaultdict(list)
        for v in company.vouchers:
            for t in v.transactions:
                by_account[t.account].append(t.amount)

        findings: list[Finding] = []
        for account_nr, amounts in by_account.items():
            if len(amounts) < 4:
                continue
            round_count = sum(1 for a in amounts if a != 0 and abs(a) % divisor_oren == 0)
            ratio = round_count / len(amounts)
            if ratio > min_ratio:
                findings.append(self._create_finding(
                    category="round_amounts",
                    risk_level="LOW",
                    summary=(
                        f"{company.org_nr}: Konto {account_nr} har {ratio:.0%} "
                        f"jämna belopp (delbart med {divisor_oren // 100:,} kr)"
                    ),
                    companies=[company.org_nr],
                    details={
                        "org_nr": company.org_nr,
                        "account": account_nr,
                        "round_ratio": round(ratio, 4),
                        "round_count": round_count,
                        "total_count": len(amounts),
                        "divisor_kr": divisor_oren // 100,
                    },
                ))
        return findings
