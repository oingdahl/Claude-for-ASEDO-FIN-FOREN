"""Tester för src/output/ — report_generator och dashboard."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from src.core.csv_importer import import_bank_csv
from src.core.data_validator import validate_bank_csv, validate_sie4
from src.core.models import Company, Finding, Transaction, Voucher
from src.core.sie_parser import parse_sie4
from src.modules.bank_matching import BankMatchingModule
from src.modules.amount_patterns import AmountPatternsModule
from src.output.dashboard import generate_dashboard
from src.output.report_generator import (
    generate_context_json,
    generate_results_json,
    save_results,
)

SIE_FILE = "test_data/SIE4_Exempelfil.SE"
CSV_FILE = "test_data/Sophone_2025_transar.csv"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sample_findings() -> list[Finding]:
    company = parse_sie4(SIE_FILE)
    transactions = import_bank_csv(CSV_FILE)
    config = {"matching": {"amount_tolerance_bank_kr": 50}}
    module = BankMatchingModule()
    return module.analyze([company], {company.org_nr: transactions}, config)


@pytest.fixture(scope="module")
def sample_company() -> Company:
    return parse_sie4(SIE_FILE)


@pytest.fixture(scope="module")
def validation_reports(sample_company):
    company = sample_company
    transactions = import_bank_csv(CSV_FILE)
    r1 = validate_sie4(company, SIE_FILE)
    r2 = validate_bank_csv(transactions, CSV_FILE)
    return [r1, r2]


@pytest.fixture(scope="module")
def results_dict(sample_findings, sample_company, validation_reports):
    return generate_results_json(
        sample_findings, [sample_company], validation_reports
    )


# ---------------------------------------------------------------------------
# generate_results_json
# ---------------------------------------------------------------------------

def test_results_json_har_schema_version(results_dict: dict) -> None:
    assert results_dict["schema_version"] == "1.0"


def test_results_json_har_generated_at(results_dict: dict) -> None:
    assert "generated_at" in results_dict
    # Ska vara parsbart som ISO-datetime
    datetime.fromisoformat(results_dict["generated_at"])


def test_results_json_har_findings(results_dict: dict) -> None:
    assert "findings" in results_dict
    assert isinstance(results_dict["findings"], list)
    assert len(results_dict["findings"]) > 0


def test_results_json_finding_har_alla_falt(results_dict: dict) -> None:
    f = results_dict["findings"][0]
    for field in ("finding_id", "module", "risk_level", "category", "summary",
                  "companies", "details", "ai_reasoning", "status"):
        assert field in f, f"Fält '{field}' saknas i finding"


def test_results_json_summary_falt(results_dict: dict) -> None:
    s = results_dict["summary"]
    assert "total_findings" in s
    assert "by_risk_level" in s
    assert "by_module" in s
    assert "by_company" in s
    assert s["total_findings"] == len(results_dict["findings"])


def test_results_json_companies(results_dict: dict) -> None:
    assert len(results_dict["companies"]) >= 1
    c = results_dict["companies"][0]
    assert "org_nr" in c
    assert "voucher_count" in c
    assert "source_checksum" in c


def test_results_json_validation_reports(results_dict: dict) -> None:
    assert len(results_dict["validation_reports"]) == 2
    for r in results_dict["validation_reports"]:
        assert "is_valid" in r
        assert "checksum" in r


def test_results_json_ar_json_serialiserbar(results_dict: dict) -> None:
    serialized = json.dumps(results_dict, ensure_ascii=False, default=str)
    reloaded = json.loads(serialized)
    assert reloaded["summary"]["total_findings"] == results_dict["summary"]["total_findings"]


# ---------------------------------------------------------------------------
# generate_context_json
# ---------------------------------------------------------------------------

def test_context_json_har_role_instruction(sample_findings, sample_company) -> None:
    ctx = generate_context_json(sample_findings, [sample_company])
    assert "role_instruction" in ctx
    assert len(ctx["role_instruction"]) > 20


def test_context_json_har_sphere_summary(sample_findings, sample_company) -> None:
    ctx = generate_context_json(sample_findings, [sample_company])
    assert "sphere_summary" in ctx
    assert ctx["sphere_summary"]["company_count"] == 1


def test_context_json_har_findings(sample_findings, sample_company) -> None:
    ctx = generate_context_json(sample_findings, [sample_company])
    assert len(ctx["findings"]) == len(sample_findings)
    f = ctx["findings"][0]
    for field in ("finding_id", "risk_level", "category", "summary"):
        assert field in f


def test_context_json_har_open_questions(sample_findings, sample_company) -> None:
    ctx = generate_context_json(sample_findings, [sample_company])
    assert "open_questions" in ctx
    assert isinstance(ctx["open_questions"], list)


# ---------------------------------------------------------------------------
# save_results
# ---------------------------------------------------------------------------

def test_save_results_skapar_filer(tmp_path, results_dict, sample_findings, sample_company) -> None:
    ctx = generate_context_json(sample_findings, [sample_company])
    save_results(str(tmp_path / "output"), results_dict, ctx)

    assert (tmp_path / "output" / "results.json").exists()
    assert (tmp_path / "output" / "context.json").exists()


def test_save_results_json_ar_giltig(tmp_path, results_dict, sample_findings, sample_company) -> None:
    ctx = generate_context_json(sample_findings, [sample_company])
    save_results(str(tmp_path / "output"), results_dict, ctx)

    results_loaded = json.loads((tmp_path / "output" / "results.json").read_text())
    context_loaded = json.loads((tmp_path / "output" / "context.json").read_text())

    assert results_loaded["schema_version"] == "1.0"
    assert "role_instruction" in context_loaded


# ---------------------------------------------------------------------------
# generate_dashboard
# ---------------------------------------------------------------------------

def test_dashboard_skapas(tmp_path, results_dict) -> None:
    out = str(tmp_path / "dashboard.html")
    generate_dashboard(results_dict, out)
    assert Path(out).exists()
    assert Path(out).stat().st_size > 1000


def test_dashboard_innehaller_json(tmp_path, results_dict) -> None:
    out = str(tmp_path / "dashboard.html")
    generate_dashboard(results_dict, out)
    html = Path(out).read_text(encoding="utf-8")
    assert "const DATA =" in html
    # Extrahera och verifiera JSON — DATA avslutas med semikolon+radbrytning+tom rad
    start = html.index("const DATA =") + len("const DATA =")
    # Hitta nästa rad som börjar med "function" (alltid efter DATA-tilldelningen)
    end = html.index("\nfunction ", start)
    json_str = html[start:end].strip().rstrip(";")
    data = json.loads(json_str)
    assert data["summary"]["total_findings"] == results_dict["summary"]["total_findings"]


def test_dashboard_innehaller_vyer(tmp_path, results_dict) -> None:
    out = str(tmp_path / "dashboard.html")
    generate_dashboard(results_dict, out)
    html = Path(out).read_text(encoding="utf-8")
    assert "Översikt" in html
    assert "Datakvalitet" in html
    assert "Fynd" in html
    assert "Bolag" in html


def test_dashboard_farger_for_risknivaer(tmp_path, results_dict) -> None:
    out = str(tmp_path / "dashboard.html")
    generate_dashboard(results_dict, out)
    html = Path(out).read_text(encoding="utf-8")
    assert "#e74c3c" in html   # HIGH (röd)
    assert "#e67e22" in html   # MEDIUM (orange)


# ---------------------------------------------------------------------------
# Integration: fynd per modul mot riktiga filer
# ---------------------------------------------------------------------------

def test_fynd_per_modul_integration(results_dict: dict) -> None:
    """Verifiera att findings innehåller rimliga kategorier."""
    by_module = results_dict["summary"]["by_module"]
    print(f"\nFynd per modul: {by_module}")
    # bank_matching ska ha fynd (SIE4 och CSV matchar inte)
    assert by_module.get("bank_matching", 0) > 0
    # Totalt ska vara > 0
    assert results_dict["summary"]["total_findings"] > 0
