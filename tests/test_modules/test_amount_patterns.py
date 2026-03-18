"""Tester för src/modules/amount_patterns.py (Modul D)."""

from __future__ import annotations

import math
import random
from datetime import date

import pytest

from src.core.models import Company, Transaction, Voucher
from src.modules.amount_patterns import AmountPatternsModule

ORG = "556001-0001"
BASE_CONFIG: dict = {"amount_patterns": {}}


def make_company(amounts_oren: list[int], texts: list[str] | None = None) -> Company:
    c = Company(org_nr=ORG, name="Testbolag AB", role="dotterbolag")
    for i, amt in enumerate(amounts_oren):
        v = Voucher(series="A", number=i + 1, date=date(2025, 1, 1), text="Ver")
        text = texts[i] if texts else f"Post {i}"
        v.transactions.append(Transaction(account="1930", amount=amt, text=text))
        c.vouchers.append(v)
    return c


# ---------------------------------------------------------------------------
# Benfords lag
# ---------------------------------------------------------------------------

def test_uniform_fordelning_ger_benford_fynd() -> None:
    """Uniform fördelning (alla siffror 1-9 lika vanliga) → benford_deviation HIGH."""
    random.seed(0)
    # ~11 transaktioner per siffra, totalt 99
    amounts = []
    for d in range(1, 10):
        for _ in range(11):
            # Belopp med given första siffra, i öre (t.ex. 1 000 – 1 999 kr = 100 000 – 199 900 öre)
            amounts.append(d * 100_000 + random.randint(0, 99_900))
    company = make_company(amounts)

    module = AmountPatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    benford = [f for f in findings if f.category == "benford_deviation"]
    assert len(benford) >= 1, "Uniform fördelning ska ge Benford-avvikelse"
    assert benford[0].risk_level == "HIGH"
    assert benford[0].details["p_value"] < 0.05


def test_benford_fordelning_ger_inga_fynd() -> None:
    """Korrekt Benford-fördelad data → p >= 0.05 → inga benford_deviation-fynd."""
    random.seed(42)
    benford_probs = [math.log10(1 + 1 / d) for d in range(1, 10)]
    # Generera 200 belopp med Benford-fördelad första siffra
    amounts = []
    for _ in range(200):
        d = random.choices(range(1, 10), weights=benford_probs)[0]
        amounts.append(d * 100_000 + random.randint(1_000, 99_000))
    company = make_company(amounts)

    module = AmountPatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    benford = [f for f in findings if f.category == "benford_deviation"]
    assert benford == [], (
        f"Benford-data fick p={[f.details['p_value'] for f in findings if f.category == 'benford_deviation']}"
    )


def test_benford_hoppas_over_vid_for_fa_poster() -> None:
    """< 50 transaktioner → Benford-testet körs INTE (inga undantag, inga fynd)."""
    # 30 uniform-distribuerade transaktioner
    amounts = [d * 100_000 for d in range(1, 10)] * 3  # 27 poster
    company = make_company(amounts)

    module = AmountPatternsModule()
    # Ska inte kasta undantag
    findings = module.analyze([company], {}, BASE_CONFIG)

    benford = [f for f in findings if f.category == "benford_deviation"]
    assert benford == [], "Benford-test ska inte köras med <50 poster"


def test_benford_details_innehaller_chi2_och_p() -> None:
    """Finding.details ska ha chi2_statistic, p_value och n."""
    random.seed(0)
    amounts = [d * 100_000 for d in range(1, 10)] * 12  # 108 poster, uniform
    company = make_company(amounts)

    module = AmountPatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    benford = [f for f in findings if f.category == "benford_deviation"]
    assert benford, "Behöver ett Benford-fynd för detta test"
    f = benford[0]
    assert "chi2_statistic" in f.details
    assert "p_value" in f.details
    assert "n" in f.details


# ---------------------------------------------------------------------------
# Belopp strax under gränsvärden
# ---------------------------------------------------------------------------

def test_5_poster_strax_under_10000_kr_flaggas() -> None:
    """5 transaktioner på 9 800 kr (inom 5% av 10 000 kr) → below_threshold MEDIUM."""
    # 9 800 kr = 980 000 öre; 5% under 10 000 kr = 9 500 kr; 9 800 kr ∈ [9 500, 10 000)
    amounts = [980_000] * 5  # 5 st à 9 800 kr
    company = make_company(amounts)

    module = AmountPatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    below = [f for f in findings if f.category == "below_threshold"]
    assert len(below) >= 1
    assert any(f.details["threshold_kr"] == 10_000 for f in below)
    assert all(f.risk_level == "MEDIUM" for f in below)


def test_3_poster_strax_under_grans_flaggas_inte() -> None:
    """Exakt 3 poster (= min_count, inte >3) → inga fynd."""
    amounts = [980_000] * 3
    company = make_company(amounts)

    module = AmountPatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    below = [f for f in findings if f.category == "below_threshold" and f.details["threshold_kr"] == 10_000]
    assert below == []


def test_belopp_over_grans_ej_flaggat() -> None:
    """Belopp exakt på gränsen (10 000 kr = threshold) → inte below_threshold."""
    amounts = [1_000_000] * 6  # 10 000 kr exakt
    company = make_company(amounts)

    module = AmountPatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    below = [f for f in findings if f.category == "below_threshold" and f.details["threshold_kr"] == 10_000]
    assert below == []


# ---------------------------------------------------------------------------
# Upprepade belopp
# ---------------------------------------------------------------------------

def test_8_poster_samma_belopp_och_text_flaggas() -> None:
    """8 transaktioner med exakt samma belopp och text → repeated_amount MEDIUM."""
    amounts = [450_000] * 8       # 4 500 kr × 8
    texts = ["Leverantör X"] * 8
    company = make_company(amounts, texts)

    module = AmountPatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    repeated = [f for f in findings if f.category == "repeated_amount"]
    assert len(repeated) >= 1
    assert repeated[0].risk_level == "MEDIUM"
    assert repeated[0].details["count"] == 8


def test_5_poster_flaggas_inte_vid_default_troskel() -> None:
    """5 poster (= default min 5, ej >5) → inga fynd."""
    amounts = [450_000] * 5
    texts = ["Leverantör Y"] * 5
    company = make_company(amounts, texts)

    module = AmountPatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    repeated = [f for f in findings if f.category == "repeated_amount"
                and f.details.get("text") == "Leverantör Y"]
    assert repeated == []


def test_samma_belopp_olika_text_flaggas_ej_gemensamt() -> None:
    """Samma belopp men olika texter → separata grupper (ingen flaggas om var för sig <5)."""
    amounts = [450_000] * 4 + [450_000] * 4
    texts = ["Text A"] * 4 + ["Text B"] * 4
    company = make_company(amounts, texts)

    module = AmountPatternsModule()
    findings = module.analyze([company], {}, BASE_CONFIG)

    repeated = [f for f in findings if f.category == "repeated_amount"]
    assert repeated == []
