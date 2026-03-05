"""Genererar resultatfiler (JSON) från analysresultat.

Funktioner:
  generate_results_json  →  Komplett resultat-dict (alla fynd + metadata)
  generate_context_json  →  Kontextfil för Claude-konversation
  save_results           →  Spara båda JSON-filerna till disk
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from src.core.data_validator import ValidationReport
from src.core.models import Company, Finding

logger = logging.getLogger(__name__)

_RISK_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]


# ---------------------------------------------------------------------------
# Serializering
# ---------------------------------------------------------------------------

def _finding_to_dict(f: Finding) -> dict:
    return {
        "finding_id": f.finding_id,
        "module": f.module,
        "companies": f.companies,
        "risk_level": f.risk_level,
        "category": f.category,
        "summary": f.summary,
        "details": f.details,
        "ai_reasoning": f.ai_reasoning,
        "status": f.status,
    }


def _company_to_dict(c: Company) -> dict:
    return {
        "org_nr": c.org_nr,
        "name": c.name,
        "role": c.role,
        "fiscal_years": [
            {
                "year_index": fy.year_index,
                "start_date": str(fy.start_date),
                "end_date": str(fy.end_date),
            }
            for fy in c.fiscal_years
        ],
        "voucher_count": len(c.vouchers),
        "transaction_count": sum(len(v.transactions) for v in c.vouchers),
        "account_count": len(c.chart_of_accounts),
        "source_file": c.source_file,
        "source_checksum": c.source_checksum,
    }


def _validation_report_to_dict(r: ValidationReport) -> dict:
    return {
        "file_path": r.file_path,
        "file_type": r.file_type,
        "checksum": r.checksum,
        "timestamp": r.timestamp.isoformat(timespec="seconds"),
        "total_records": r.total_records,
        "parsed_records": r.parsed_records,
        "warnings": r.warnings,
        "errors": r.errors,
        "is_valid": r.is_valid,
        "details": r.details,
    }


# ---------------------------------------------------------------------------
# Publik API
# ---------------------------------------------------------------------------

def generate_results_json(
    findings: list[Finding],
    companies: list[Company],
    validation_reports: list[ValidationReport],
) -> dict:
    """Genererar komplett resultat-dict med alla fynd och metadata.

    Returns:
        Dict klar att serialiseras med json.dumps.
    """
    by_risk: dict[str, int] = defaultdict(int)
    by_module: dict[str, int] = defaultdict(int)
    by_company: dict[str, int] = defaultdict(int)
    by_category: dict[str, int] = defaultdict(int)

    for f in findings:
        by_risk[f.risk_level] += 1
        by_module[f.module] += 1
        by_category[f.category] += 1
        for org in f.companies:
            by_company[org] += 1

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "schema_version": "1.0",
        "companies": [_company_to_dict(c) for c in companies],
        "validation_reports": [_validation_report_to_dict(r) for r in validation_reports],
        "findings": [_finding_to_dict(f) for f in findings],
        "summary": {
            "total_findings": len(findings),
            "by_risk_level": {
                level: by_risk.get(level, 0) for level in _RISK_ORDER
            },
            "by_module": dict(sorted(by_module.items())),
            "by_company": dict(sorted(by_company.items())),
            "by_category": dict(sorted(by_category.items())),
        },
    }


def generate_context_json(
    findings: list[Finding],
    companies: list[Company],
) -> dict:
    """Genererar kontextfil optimerad för att ladda in i Claude-konversation.

    Kontextfilen inkluderar:
    - Sfärsammanfattning
    - Alla fynd med risk_level, summary och ai_reasoning
    - Öppna frågor
    - Instruktion till Claude

    Returns:
        Dict klar att serialiseras med json.dumps.
    """
    # Sfärsammanfattning
    total_debit = sum(
        t.amount for c in companies
        for v in c.vouchers
        for t in v.transactions
        if t.amount < 0
    )
    total_credit = sum(
        t.amount for c in companies
        for v in c.vouchers
        for t in v.transactions
        if t.amount > 0
    )

    sphere_summary = {
        "company_count": len(companies),
        "companies": [
            {
                "org_nr": c.org_nr,
                "name": c.name,
                "role": c.role,
                "periods": [
                    f"{fy.start_date}–{fy.end_date}" for fy in c.fiscal_years
                ],
                "voucher_count": len(c.vouchers),
            }
            for c in companies
        ],
        "total_debit_kr": total_debit / 100,
        "total_credit_kr": total_credit / 100,
    }

    # Fynd sorterade på risk
    risk_rank = {level: i for i, level in enumerate(_RISK_ORDER)}
    sorted_findings = sorted(findings, key=lambda f: risk_rank.get(f.risk_level, 99))

    # Öppna frågor: HIGH/CRITICAL utan ai_reasoning
    open_questions = [
        f"Granska fynd {f.finding_id} ({f.category}): {f.summary}"
        for f in sorted_findings
        if f.risk_level in ("CRITICAL", "HIGH") and not f.ai_reasoning
    ]

    return {
        "role_instruction": (
            "Du är en forensisk revisorsassistent med expertis i svensk redovisning "
            "och bolagsrätt. Du hjälper till att granska och fördjupa analysen av "
            "forensiska fynd i ett bolags bokföring. Svara alltid på svenska. "
            "Var konkret och hänvisa till specifika fynd när du resonerar."
        ),
        "sphere_summary": sphere_summary,
        "findings": [
            {
                "finding_id": f.finding_id,
                "module": f.module,
                "risk_level": f.risk_level,
                "category": f.category,
                "summary": f.summary,
                "ai_reasoning": f.ai_reasoning or "",
                "status": f.status,
                "companies": f.companies,
            }
            for f in sorted_findings
        ],
        "open_questions": open_questions[:20],  # Max 20 öppna frågor
        "data_references": [
            {"org_nr": c.org_nr, "name": c.name, "source_file": c.source_file}
            for c in companies
        ],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def save_results(output_dir: str, results: dict, context: dict) -> None:
    """Sparar results.json och context.json till output-mappen.

    Args:
        output_dir: Mapp att spara i (skapas om den inte finns).
        results: Dict från generate_results_json.
        context: Dict från generate_context_json.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    results_path = out / "results.json"
    context_path = out / "context.json"

    results_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    context_path.write_text(
        json.dumps(context, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    logger.info(
        "Sparade %d fynd till %s och %s",
        len(results.get("findings", [])),
        results_path,
        context_path,
    )
