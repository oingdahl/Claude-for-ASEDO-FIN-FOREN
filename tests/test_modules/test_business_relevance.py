"""Tester för src/modules/business_relevance.py (Modul G)."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src.core.models import Company, Transaction, Voucher
from src.modules.business_relevance import (
    AllabolagetProvider,
    BusinessRelevanceModule,
    CompanyInfo,
    MockLookupProvider,
    _is_it_related,
    _normalize_name,
)

ORG = "556001-0001"


def make_company(texts: list[str]) -> Company:
    """Bolag med verifikationer vars texter är motpartsbeskrivningarna."""
    c = Company(org_nr=ORG, name="IT-bolaget AB", role="dotterbolag")
    for i, text in enumerate(texts, start=1):
        v = Voucher(series="A", number=i, date=__import__("datetime").date(2025, 1, i), text=text)
        v.transactions.append(Transaction(account="1930", amount=-(i * 100_000)))
        c.vouchers.append(v)
    return c


# ---------------------------------------------------------------------------
# MockLookupProvider
# ---------------------------------------------------------------------------

IT_COMPANY = CompanyInfo(
    org_nr="556001-1001",
    name="IT-leverantören AB",
    sni_code="62010",
    description="Dataprogrammering och IT-tjänster",
    status="active",
)
FLOWER_SHOP = CompanyInfo(
    org_nr="556002-2002",
    name="Blomsterhandeln AB",
    sni_code="47760",
    description="Handel med blommor och växter i butik",
    status="active",
)
UNKNOWN = CompanyInfo(None, "Okänt Bolag", None, None, "unknown")


def make_mock_provider() -> MockLookupProvider:
    return MockLookupProvider({
        "IT-leverantören": IT_COMPANY,
        "Blomsterhandeln": FLOWER_SHOP,
        # "Okänt Bolag" → default unknown
    })


# ---------------------------------------------------------------------------
# IT-relaterad bedömning
# ---------------------------------------------------------------------------

def test_it_sni_kod_bedöms_som_it() -> None:
    assert _is_it_related(IT_COMPANY) is True


def test_blomsterhandel_ej_it_relaterad() -> None:
    assert _is_it_related(FLOWER_SHOP) is False


def test_it_nyckelord_i_description() -> None:
    info = CompanyInfo(None, "TestAB", "99", "Levererar mjukvara och system", "active")
    assert _is_it_related(info) is True


# ---------------------------------------------------------------------------
# Namnormalisering
# ---------------------------------------------------------------------------

def test_normalize_tar_bort_ab_suffix() -> None:
    assert _normalize_name("IT-leverantören AB") == "IT-leverantören"


def test_normalize_trimmar_mellanslag() -> None:
    assert _normalize_name("  Blomsterhandeln  ") == "Blomsterhandeln"


def test_normalize_bevarar_namn_utan_suffix() -> None:
    assert _normalize_name("Okänt Bolag") == "Okänt Bolag"


# ---------------------------------------------------------------------------
# Fynd: blomsterhandel → unrelated_business
# ---------------------------------------------------------------------------

def test_blomsterhandel_ger_unrelated_business() -> None:
    """Blomsterhandel utan IT-koppling → unrelated_business MEDIUM."""
    provider = make_mock_provider()
    company = make_company(["Blomsterhandeln AB"])

    module = BusinessRelevanceModule(lookup_provider=provider)
    findings = module.analyze([company], {}, {})

    unrelated = [f for f in findings if f.category == "unrelated_business"]
    assert len(unrelated) >= 1
    assert unrelated[0].risk_level == "MEDIUM"
    assert "Blomsterhandeln" in unrelated[0].summary


def test_okant_bolag_ger_unknown_counterparty() -> None:
    """Okänd motpart → unknown_counterparty MEDIUM."""
    provider = make_mock_provider()
    company = make_company(["Okänt Bolag XYZ"])

    module = BusinessRelevanceModule(lookup_provider=provider)
    findings = module.analyze([company], {}, {})

    unknown = [f for f in findings if f.category == "unknown_counterparty"]
    assert len(unknown) >= 1
    assert unknown[0].risk_level == "MEDIUM"


def test_it_bolag_inga_fynd() -> None:
    """Känt IT-bolag → inga fynd."""
    provider = make_mock_provider()
    company = make_company(["IT-leverantören AB"])

    module = BusinessRelevanceModule(lookup_provider=provider)
    findings = module.analyze([company], {}, {})

    assert findings == []


# ---------------------------------------------------------------------------
# Cache-test: AllabolagetProvider
# ---------------------------------------------------------------------------

def test_allabolag_cache_hit_inget_http(tmp_path: pytest.TempPathFactory) -> None:
    """Förfylld cache → lookup() gör inget HTTP-anrop."""
    cache_data = {
        "CachedAB": {
            "org_nr": "556677-8899",
            "sni_code": "62010",
            "description": "IT-tjänster",
            "status": "active",
            "cached_at": datetime.now().isoformat(timespec="seconds"),
        }
    }
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

    provider = AllabolagetProvider(cache_file=str(cache_file))
    result = provider.lookup("CachedAB")

    assert result.org_nr == "556677-8899"
    assert result.status == "active"
    assert result.sni_code == "62010"


def test_allabolag_gammal_cache_ignoreras(tmp_path: pytest.TempPathFactory) -> None:
    """Cache-post äldre än max_age ska ignoreras och ge HTTP-anrop (eller unknown vid nätverksfel)."""
    cache_data = {
        "GammaltBolag": {
            "org_nr": "556000-0000",
            "sni_code": "99",
            "description": "Gammal data",
            "status": "active",
            "cached_at": "2020-01-01T00:00:00",  # Mycket gammal
        }
    }
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

    # Inget nätverk i tester → förväntar oss status "unknown" (HTTP-fel)
    provider = AllabolagetProvider(
        cache_file=str(cache_file),
        request_delay_s=0,  # Inget fördröjning i test
        cache_max_age_days=30,
    )
    result = provider.lookup("GammaltBolag")
    # Ska INTE returnera gammal cache — statusen blir "unknown" (nätverksfel)
    assert result.status == "unknown"


# ---------------------------------------------------------------------------
# Claude API-mock
# ---------------------------------------------------------------------------

def test_ai_bedomning_blomsterhandel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude svarar 'relevant: false' → unrelated_business."""
    mock_response = MagicMock()
    mock_response.content = [
        MagicMock(text='{"relevant": false, "reasoning": "Blomsterhandel ej IT"}')
    ]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    provider = MockLookupProvider({"Blomsterhandeln": FLOWER_SHOP})
    company = make_company(["Blomsterhandeln AB"])

    module = BusinessRelevanceModule(lookup_provider=provider, ai_client=mock_client)
    findings = module.analyze([company], {}, {})

    unrelated = [f for f in findings if f.category == "unrelated_business"]
    assert len(unrelated) >= 1
    assert unrelated[0].details["ai_relevant"] is False
    assert "Blomsterhandel ej IT" in unrelated[0].details["ai_reasoning"]
    mock_client.messages.create.assert_called_once()


def test_ai_bedomning_relevant_inga_fynd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude svarar 'relevant: true' → inga fynd."""
    mock_response = MagicMock()
    mock_response.content = [
        MagicMock(text='{"relevant": true, "reasoning": "IT-koppling finns"}')
    ]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    # Bolag utan känd IT-koppling men Claude säger OK
    provider = MockLookupProvider({
        "Okänd konsult": CompanyInfo(
            "556999-0000", "Okänd konsult AB", "74900",
            "Konsulttjänster", "active"
        )
    })
    company = make_company(["Okänd konsult AB"])

    module = BusinessRelevanceModule(lookup_provider=provider, ai_client=mock_client)
    findings = module.analyze([company], {}, {})

    unrelated = [f for f in findings if f.category == "unrelated_business"]
    assert unrelated == []


# ---------------------------------------------------------------------------
# Modulegenskaper
# ---------------------------------------------------------------------------

def test_modul_egenskaper() -> None:
    m = BusinessRelevanceModule(lookup_provider=MockLookupProvider({}))
    assert m.name == "business_relevance"
    assert len(m.description) > 10
