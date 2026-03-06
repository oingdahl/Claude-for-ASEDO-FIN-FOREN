"""
Forensisk Bokföringsanalys — Huvudskript

Användning:
  python main.py --config config/wooz_config.yaml \\
                 --sie-dir ./data/sie/ \\
                 --csv-dir ./data/csv/ \\
                 --output ./output/

  # Utan AI (kör alla moduler direkt):
  python main.py --config config/default_config.yaml \\
                 --sie-dir test_data/ --csv-dir test_data/ \\
                 --output test_output/ --no-ai
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from src.core.csv_importer import CSVImportError, import_bank_csv
from src.core.data_validator import (
    ValidationReport,
    print_validation_summary,
    validate_bank_csv,
    validate_sie4,
)
from src.core.models import BankTransaction, Company
from src.core.sie_parser import SIEParseError, parse_sie4
from src.modules.amount_patterns import AmountPatternsModule
from src.modules.bank_matching import BankMatchingModule
from src.modules.business_relevance import BusinessRelevanceModule, MockLookupProvider
from src.modules.clearing_accounts import ClearingAccountsModule
from src.modules.intercompany import IntercompanyModule
from src.modules.salary_expenses import SalaryExpensesModule
from src.modules.time_patterns import TimePatternsModule
from src.orchestrator.engine import AnalysisResult, OrchestrationEngine
from src.output.dashboard import generate_dashboard
from src.output.report_generator import generate_context_json, generate_results_json, save_results

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_SIE_EXTENSIONS = {".se", ".sie", ".si"}

_ALL_MODULES = [
    "bank_matching",
    "intercompany",
    "clearing_accounts",
    "amount_patterns",
    "time_patterns",
    "salary_expenses",
    "business_relevance",
]


# ---------------------------------------------------------------------------
# Fil-inläsning
# ---------------------------------------------------------------------------

def _load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    return _normalize_config(config or {})


def _normalize_config(config: dict) -> dict:
    """Konverterar flat default_config-format till det format modulerna förväntar sig.

    default_config.yaml lagrar 'clearing_accounts' som en platt lista med kontonummer,
    men ClearingAccountsModule förväntar sig en dict med undernycklar.
    """
    normalized = dict(config)

    if isinstance(config.get("clearing_accounts"), list):
        clearing_cfg = config.get("clearing", {})
        owner_loan_acct = clearing_cfg.get("owner_loan_account", "2393")
        normalized["clearing_accounts"] = {
            "accounts": [str(a) for a in config["clearing_accounts"]],
            "owner_loan_accounts": [owner_loan_acct],
            "owner_loan_large_kr": clearing_cfg.get("owner_loan_high_threshold_kr", 50_000),
            "old_balance_days": clearing_cfg.get("old_balance_days", 90),
        }

    return normalized


def _parse_sie_files(sie_dir: str) -> list[Company]:
    """Parsa alla SIE4-filer i angiven mapp. Filer med fel hoppas över."""
    companies: list[Company] = []
    for filepath in sorted(Path(sie_dir).iterdir()):
        if filepath.suffix.lower() not in _SIE_EXTENSIONS:
            continue
        try:
            company = parse_sie4(str(filepath))
            company.source_file = str(filepath)
            companies.append(company)
            logger.info("Parsade SIE4: %s → %s (%s)", filepath.name, company.name, company.org_nr)
        except SIEParseError as exc:
            logger.error("Kunde inte parsa SIE4-fil %s: %s", filepath.name, exc)
    return companies


def _import_csv_files(csv_dir: str, config: dict) -> dict[str, list[BankTransaction]]:
    """Importera alla CSV-filer i mappen. Returnerar dict filnamn → transaktioner."""
    csv_transactions: dict[str, list[BankTransaction]] = {}
    for filepath in sorted(Path(csv_dir).iterdir()):
        if filepath.suffix.lower() != ".csv":
            continue
        try:
            txs = import_bank_csv(str(filepath), config)
            csv_transactions[filepath.name] = txs
            logger.info("Importerade CSV: %s → %d transaktioner", filepath.name, len(txs))
        except CSVImportError as exc:
            logger.error("Kunde inte importera CSV %s: %s", filepath.name, exc)
    return csv_transactions


# ---------------------------------------------------------------------------
# Fil-till-bolag-mappning
# ---------------------------------------------------------------------------

def _org_nr_variants(org_nr: str) -> set[str]:
    """Returnerar org_nr med och utan bindestreck."""
    plain = org_nr.replace("-", "")
    return {plain, org_nr}


def _match_csv_to_companies(
    companies: list[Company],
    csv_transactions: dict[str, list[BankTransaction]],
    config: dict,
) -> dict[str, list[BankTransaction]]:
    """Returnerar dict org_nr → lista av BankTransaction (kan vara tom).

    Prioritetsordning:
    1. Filnamnet innehåller org_nr (med eller utan bindestreck)
    2. Explicit mappning i config['file_mappings']
    3. Fallback: org_nr får tom lista (Modul A hoppas över)
    """
    file_mappings: dict[str, str] = config.get("file_mappings", {}) or {}
    # Bygg inverterad mappning: filnamn → org_nr
    explicit: dict[str, str] = {v: k for k, v in file_mappings.items()}

    result: dict[str, list[BankTransaction]] = {c.org_nr: [] for c in companies}
    claimed: set[str] = set()

    for company in companies:
        variants = _org_nr_variants(company.org_nr)

        # Steg 1: automatisk matchning via filnamn
        for filename, txs in csv_transactions.items():
            if filename in claimed:
                continue
            if any(v in filename for v in variants):
                result[company.org_nr] = txs
                claimed.add(filename)
                logger.info("Matchade %s → %s (via org_nr i filnamn)", filename, company.org_nr)
                break
        else:
            # Steg 2: explicit config-mappning
            mapped_file = file_mappings.get(company.org_nr)
            if mapped_file and mapped_file in csv_transactions and mapped_file not in claimed:
                result[company.org_nr] = csv_transactions[mapped_file]
                claimed.add(mapped_file)
                logger.info("Matchade %s → %s (via file_mappings)", mapped_file, company.org_nr)
            else:
                # Steg 3: fallback
                logger.warning(
                    "Ingen bank-CSV hittad för %s (%s) — Modul A hoppas över",
                    company.name,
                    company.org_nr,
                )

    # Logga CSV-filer som inte matchade något bolag
    for filename in csv_transactions:
        if filename not in claimed:
            logger.warning("CSV-fil utan matchat bolag: %s", filename)

    return result


# ---------------------------------------------------------------------------
# Validering
# ---------------------------------------------------------------------------

def _validate_all(
    companies: list[Company],
    csv_transactions: dict[str, list[BankTransaction]],
    bank_tx_by_org: dict[str, list[BankTransaction]],
    config: dict,
) -> list[ValidationReport]:
    """Kör validering på alla SIE4-filer och bank-CSV-filer."""
    reports: list[ValidationReport] = []

    for company in companies:
        report = validate_sie4(company, company.source_file)
        reports.append(report)

    # Validera CSV-filer matchade mot bolag
    org_to_csv_file: dict[str, str] = {}
    for company in companies:
        variants = _org_nr_variants(company.org_nr)
        for filename in csv_transactions:
            if any(v in filename for v in variants):
                org_to_csv_file[company.org_nr] = filename
                break

    for company in companies:
        csv_file = org_to_csv_file.get(company.org_nr)
        if csv_file:
            txs = bank_tx_by_org.get(company.org_nr, [])
            report = validate_bank_csv(txs, csv_file)
            reports.append(report)

    return reports


# ---------------------------------------------------------------------------
# Körning utan AI
# ---------------------------------------------------------------------------

def _run_all_modules(
    companies: list[Company],
    bank_tx_by_org: dict[str, list[BankTransaction]],
    config: dict,
    module_filter: list[str] | None,
) -> list:
    """Kör valda moduler direkt (utan OrchestrationEngine)."""
    active = module_filter or _ALL_MODULES

    modules = []
    if "bank_matching" in active:
        modules.append(BankMatchingModule())
    if "intercompany" in active:
        modules.append(IntercompanyModule())
    if "clearing_accounts" in active:
        modules.append(ClearingAccountsModule())
    if "amount_patterns" in active:
        modules.append(AmountPatternsModule())
    if "time_patterns" in active:
        modules.append(TimePatternsModule())
    if "salary_expenses" in active:
        modules.append(SalaryExpensesModule())
    if "business_relevance" in active:
        modules.append(BusinessRelevanceModule(MockLookupProvider({})))

    all_findings = []
    for module in modules:
        try:
            findings = module.analyze(companies, bank_tx_by_org, config)
            all_findings.extend(findings)
            logger.info("Modul %-20s → %d fynd", module.name, len(findings))
        except Exception as exc:  # noqa: BLE001
            logger.error("Fel i modul %s: %s", module.name, exc)

    return all_findings


# ---------------------------------------------------------------------------
# Terminal-sammanfattning
# ---------------------------------------------------------------------------

def _print_terminal_summary(
    findings: list,
    companies: list[Company],
    validation_reports: list[ValidationReport],
    summary_text: str,
) -> None:
    print("\n" + "=" * 60)
    print("  FORENSISK BOKFÖRINGSANALYS — SAMMANFATTNING")
    print("=" * 60)
    print(f"\nBolag analyserade: {len(companies)}")
    for c in companies:
        print(f"  • {c.name} ({c.org_nr})")

    valid_count = sum(1 for r in validation_reports if r.is_valid)
    print(f"\nValidering: {valid_count}/{len(validation_reports)} filer godkända")

    print(f"\nTotalt antal fynd: {len(findings)}")
    risk_counts: Counter = Counter(f.risk_level for f in findings)
    for level in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]:
        count = risk_counts.get(level, 0)
        if count:
            print(f"  {level:<10} {count}")

    module_counts: Counter = Counter(f.module for f in findings)
    if module_counts:
        print("\nFynd per modul:")
        for module, count in sorted(module_counts.items()):
            print(f"  {module:<25} {count}")

    if summary_text:
        print("\nAI-sammanfattning:")
        print(f"  {summary_text[:300]}{'...' if len(summary_text) > 300 else ''}")

    print("\n" + "=" * 60)


# ---------------------------------------------------------------------------
# Huvudfunktion
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forensisk bokföringsanalys av bolagssfär",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", required=True, help="Sökväg till YAML-konfigurationsfil")
    parser.add_argument("--sie-dir", required=True, help="Mapp med SIE4-filer (.se/.sie)")
    parser.add_argument("--csv-dir", required=True, help="Mapp med bankutdrag (CSV)")
    parser.add_argument("--output", default="./output", help="Output-mapp (default: ./output)")
    parser.add_argument(
        "--api-key",
        default=None,
        help="Claude API-nyckel (eller sätt miljövariabeln ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Kör utan AI-resonemang (alla moduler körs direkt)",
    )
    parser.add_argument(
        "--modules",
        nargs="+",
        metavar="MODUL",
        help=f"Specifika moduler att köra (default: alla). Tillgängliga: {', '.join(_ALL_MODULES)}",
    )
    args = parser.parse_args()

    # 1. Ladda config
    logger.info("Laddar konfiguration: %s", args.config)
    config = _load_config(args.config)

    # 2. Parsa SIE4-filer
    logger.info("Parsar SIE4-filer från: %s", args.sie_dir)
    companies = _parse_sie_files(args.sie_dir)
    if not companies:
        logger.error("Inga SIE4-filer hittades i %s — avbryter.", args.sie_dir)
        sys.exit(1)

    # 3. Importera CSV-filer
    logger.info("Importerar bankutdrag från: %s", args.csv_dir)
    csv_transactions = _import_csv_files(args.csv_dir, config)

    # 4. Matcha CSV till bolag
    bank_tx_by_org = _match_csv_to_companies(companies, csv_transactions, config)

    # 5. Validering
    logger.info("Validerar data...")
    validation_reports = _validate_all(companies, csv_transactions, bank_tx_by_org, config)
    print_validation_summary(validation_reports)

    # 6. Analys
    summary_text = ""
    if args.no_ai:
        logger.info("Kör utan AI (--no-ai)")
        findings = _run_all_modules(companies, bank_tx_by_org, config, args.modules)
        result = AnalysisResult(
            findings=findings,
            companies=companies,
            validation_passed=all(r.is_valid for r in validation_reports),
        )
    else:
        api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.error(
                "Ingen API-nyckel. Sätt ANTHROPIC_API_KEY eller använd --api-key. "
                "Alternativt: kör med --no-ai."
            )
            sys.exit(1)
        logger.info("Startar AI-orkestrerare...")
        engine = OrchestrationEngine(config=config, api_key=api_key)
        result = engine.run_full_analysis(companies, bank_tx_by_org, validation_reports)
        summary_text = result.summary

    # 7. Generera output-filer
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    results_dict = generate_results_json(
        findings=result.findings,
        companies=result.companies,
        validation_reports=validation_reports,
    )
    if summary_text:
        results_dict["ai_summary"] = summary_text
    context_dict = generate_context_json(
        findings=result.findings,
        companies=result.companies,
    )
    save_results(str(output_dir), results_dict, context_dict)

    dashboard_path = str(output_dir / "dashboard.html")
    generate_dashboard(results_dict, dashboard_path)
    logger.info("Dashboard: %s", dashboard_path)

    # 8. Terminal-sammanfattning
    _print_terminal_summary(result.findings, companies, validation_reports, summary_text)

    logger.info("Klart. Output: %s", output_dir.resolve())


if __name__ == "__main__":
    main()
