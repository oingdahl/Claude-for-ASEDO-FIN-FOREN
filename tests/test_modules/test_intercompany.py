"""Tester för src/modules/intercompany.py (Modul B)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.core.models import Company, Transaction, Voucher
from src.modules.intercompany import IntercompanyModule

# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

ORG_A = "556001-0001"
ORG_B = "556002-0002"
ORG_C = "556003-0003"


def make_company(org_nr: str, name: str, balances: dict) -> Company:
    c = Company(org_nr=org_nr, name=name, role="dotterbolag")
    c.closing_balances = {k: Decimal(str(v)) for k, v in balances.items()}
    return c


def make_pair(a: str, acc_a: str, b: str, acc_b: str) -> dict:
    return {"company_a": a, "account_a": acc_a, "company_b": b, "account_b": acc_b}


def base_config(tolerance: int = 500, extra_pairs: list = None) -> dict:
    pairs = [make_pair(ORG_A, "1661", ORG_B, "2461")]
    if extra_pairs:
        pairs.extend(extra_pairs)
    return {
        "intercompany": {
            "tolerance_kr": tolerance,
            "pairs": pairs,
            "known_companies": [ORG_A, ORG_B, ORG_C],
        }
    }


# ---------------------------------------------------------------------------
# Balanserade fordringar — inga fynd
# ---------------------------------------------------------------------------

def test_balanserad_fordran_inga_fynd() -> None:
    """A fordran 100 000 kr + B skuld -100 000 kr → is_valid, inga fynd."""
    company_a = make_company(ORG_A, "Bolag A", {"1661": "100000.00"})
    company_b = make_company(ORG_B, "Bolag B", {"2461": "-100000.00"})

    module = IntercompanyModule()
    findings = module.analyze([company_a, company_b], {}, base_config())

    imbalance = [f for f in findings if f.category == "ic_imbalance"]
    assert imbalance == []


def test_exakt_noll_inga_fynd() -> None:
    """Båda konton med saldo 0 → inga fynd."""
    company_a = make_company(ORG_A, "Bolag A", {"1661": "0.00"})
    company_b = make_company(ORG_B, "Bolag B", {"2461": "0.00"})

    module = IntercompanyModule()
    findings = module.analyze([company_a, company_b], {}, base_config())

    imbalance = [f for f in findings if f.category == "ic_imbalance"]
    assert imbalance == []


# ---------------------------------------------------------------------------
# Obalans
# ---------------------------------------------------------------------------

def test_obalans_over_tolerans_flaggas_high() -> None:
    """Diff 501 kr > 500 kr tolerans → ic_imbalance HIGH."""
    company_a = make_company(ORG_A, "Bolag A", {"1661": "100501.00"})
    company_b = make_company(ORG_B, "Bolag B", {"2461": "-100000.00"})

    module = IntercompanyModule()
    findings = module.analyze([company_a, company_b], {}, base_config(tolerance=500))

    imbalance = [f for f in findings if f.category == "ic_imbalance"]
    assert len(imbalance) == 1
    assert imbalance[0].risk_level == "HIGH"
    assert imbalance[0].details["diff_kr"] == pytest.approx(501.0)


def test_obalans_detaljer_innehaller_ratt_info() -> None:
    """Finding.details ska ha org_nr, konton och diff."""
    company_a = make_company(ORG_A, "Bolag A", {"1661": "101000.00"})
    company_b = make_company(ORG_B, "Bolag B", {"2461": "-100000.00"})

    module = IntercompanyModule()
    findings = module.analyze([company_a, company_b], {}, base_config(tolerance=500))

    f = next(x for x in findings if x.category == "ic_imbalance")
    assert f.details["org_nr_a"] == ORG_A
    assert f.details["org_nr_b"] == ORG_B
    assert f.details["account_a"] == "1661"
    assert f.details["account_b"] == "2461"
    assert f.details["tolerance_kr"] == 500.0


# ---------------------------------------------------------------------------
# Toleransgräns
# ---------------------------------------------------------------------------

def test_tolerans_499_kr_inga_fynd() -> None:
    """Diff 499 kr ≤ 500 kr → ingen flagga."""
    company_a = make_company(ORG_A, "Bolag A", {"1661": "100499.00"})
    company_b = make_company(ORG_B, "Bolag B", {"2461": "-100000.00"})

    module = IntercompanyModule()
    findings = module.analyze([company_a, company_b], {}, base_config(tolerance=500))

    imbalance = [f for f in findings if f.category == "ic_imbalance"]
    assert imbalance == []


def test_tolerans_501_kr_flaggas() -> None:
    """Diff 501 kr > 500 kr → flagga."""
    company_a = make_company(ORG_A, "Bolag A", {"1661": "100501.00"})
    company_b = make_company(ORG_B, "Bolag B", {"2461": "-100000.00"})

    module = IntercompanyModule()
    findings = module.analyze([company_a, company_b], {}, base_config(tolerance=500))

    imbalance = [f for f in findings if f.category == "ic_imbalance"]
    assert len(imbalance) == 1


def test_exakt_toleransgrans_500_kr_inga_fynd() -> None:
    """Diff exakt 500 kr = tolerans → ingen flagga."""
    company_a = make_company(ORG_A, "Bolag A", {"1661": "100500.00"})
    company_b = make_company(ORG_B, "Bolag B", {"2461": "-100000.00"})

    module = IntercompanyModule()
    findings = module.analyze([company_a, company_b], {}, base_config(tolerance=500))

    imbalance = [f for f in findings if f.category == "ic_imbalance"]
    assert imbalance == []


# ---------------------------------------------------------------------------
# Ensidig post
# ---------------------------------------------------------------------------

def test_ensidig_fordran_flaggas_high() -> None:
    """A har fordran men B saknar motpost → ic_one_sided HIGH."""
    company_a = make_company(ORG_A, "Bolag A", {"1661": "50000.00"})
    company_b = make_company(ORG_B, "Bolag B", {})  # Inget konto 2461

    module = IntercompanyModule()
    findings = module.analyze([company_a, company_b], {}, base_config())

    one_sided = [f for f in findings if f.category == "ic_one_sided"]
    assert len(one_sided) >= 1
    assert all(f.risk_level == "HIGH" for f in one_sided)


def test_ic_konto_utan_par_i_config_flaggas() -> None:
    """IC-konto utan konfigurerat par → ic_one_sided."""
    # Bolag med konto 1662 som inte finns i pairs
    company = make_company(ORG_A, "Bolag A", {"1662": "25000.00"})
    company_b = make_company(ORG_B, "Bolag B", {})

    cfg = {"intercompany": {"tolerance_kr": 500, "pairs": [], "known_companies": [ORG_A, ORG_B]}}
    module = IntercompanyModule()
    findings = module.analyze([company, company_b], {}, cfg)

    one_sided = [f for f in findings if f.category == "ic_one_sided"]
    assert len(one_sided) >= 1


# ---------------------------------------------------------------------------
# Cirkelflöden
# ---------------------------------------------------------------------------

def test_cirkelflode_3_bolag_flaggas_high() -> None:
    """A→B→C→A cirkelflöde → ic_circular HIGH."""
    cfg = {
        "intercompany": {
            "tolerance_kr": 500,
            "pairs": [
                make_pair(ORG_A, "1661", ORG_B, "2461"),
                make_pair(ORG_B, "1662", ORG_C, "2462"),
                make_pair(ORG_C, "1663", ORG_A, "2463"),
            ],
            "known_companies": [ORG_A, ORG_B, ORG_C],
        }
    }
    company_a = make_company(ORG_A, "Bolag A", {"1661": "50000.00", "2463": "-50000.00"})
    company_b = make_company(ORG_B, "Bolag B", {"2461": "-50000.00", "1662": "50000.00"})
    company_c = make_company(ORG_C, "Bolag C", {"2462": "-50000.00", "1663": "50000.00"})

    module = IntercompanyModule()
    findings = module.analyze([company_a, company_b, company_c], {}, cfg)

    circular = [f for f in findings if f.category == "ic_circular"]
    assert len(circular) >= 1
    assert all(f.risk_level == "HIGH" for f in circular)
    # Alla tre bolag ska vara med
    all_companies = {org for f in circular for org in f.companies}
    assert ORG_A in all_companies
    assert ORG_B in all_companies
    assert ORG_C in all_companies


def test_ingen_cirkel_utan_cykel() -> None:
    """Enkel A→B utan återkoppling → inga ic_circular."""
    company_a = make_company(ORG_A, "Bolag A", {"1661": "50000.00"})
    company_b = make_company(ORG_B, "Bolag B", {"2461": "-50000.00"})

    module = IntercompanyModule()
    findings = module.analyze([company_a, company_b], {}, base_config())

    circular = [f for f in findings if f.category == "ic_circular"]
    assert circular == []


# ---------------------------------------------------------------------------
# Modulegenskaper
# ---------------------------------------------------------------------------

def test_modul_egenskaper() -> None:
    m = IntercompanyModule()
    assert m.name == "intercompany"
    assert len(m.description) > 10


def test_finding_ids_unika() -> None:
    """Alla finding_id ska vara unika."""
    company_a = make_company(ORG_A, "Bolag A", {"1661": "100501.00", "1662": "5000.00"})
    company_b = make_company(ORG_B, "Bolag B", {"2461": "-100000.00"})

    module = IntercompanyModule()
    findings = module.analyze([company_a, company_b], {}, base_config())

    ids = [f.finding_id for f in findings]
    assert len(ids) == len(set(ids))


def test_inga_fynd_utan_ic_konton() -> None:
    """Bolag utan IC-konton → inga fynd."""
    company_a = make_company(ORG_A, "Bolag A", {"1930": "100000.00"})
    company_b = make_company(ORG_B, "Bolag B", {"3001": "-50000.00"})

    cfg = {"intercompany": {"tolerance_kr": 500, "pairs": [], "known_companies": [ORG_A, ORG_B]}}
    module = IntercompanyModule()
    findings = module.analyze([company_a, company_b], {}, cfg)

    assert findings == []
