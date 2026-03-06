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
from src.followup.data_accessor import DataAccessor
from src.followup.investigator import Investigator
from src.followup.session import SessionManager
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
# Uppföljningsläge — hjälpfunktioner
# ---------------------------------------------------------------------------

def _load_findings_from_results(results_path: str) -> list:
    """Ladda fynd från results.json (returnerar Finding-liknande dicts)."""
    import json as _json
    from src.core.models import Finding as _Finding
    data = _json.loads(Path(results_path).read_text(encoding="utf-8"))
    findings = []
    for fd in data.get("findings", []):
        f = _Finding(
            finding_id=fd["finding_id"],
            module=fd["module"],
            companies=fd["companies"],
            risk_level=fd["risk_level"],
            category=fd["category"],
            summary=fd["summary"],
        )
        f.ai_reasoning = fd.get("ai_reasoning")
        f.status = fd.get("status", "open")
        findings.append(f)
    return findings


def _handle_builtin_command(
    cmd: str,
    investigator: Investigator,
    session_manager: SessionManager,
    output_dir: str,
) -> tuple[bool, str]:
    """Hanterar inbyggda kommandon utan API-anrop.

    Returnerar (hanterad: bool, svar: str).
    """
    parts = cmd.strip().split(None, 2)
    keyword = parts[0].lower() if parts else ""

    if keyword == "hjälp":
        return True, (
            "Tillgängliga kommandon:\n"
            "  fynd [RISKNIVÅ|MODUL]    Lista fynd\n"
            "  notera <fynd_id> <text>  Lägg till notering\n"
            "  status <fynd_id> <status> Ändra status (open/investigated/confirmed/dismissed/escalated)\n"
            "  bolag                    Lista alla bolag\n"
            "  historik                 Visa tidigare frågor\n"
            "  spara                    Spara session manuellt\n"
            "  exportera [sökväg]       Exportera utredningsjournal\n"
            "  avsluta                  Spara och avsluta"
        )

    if keyword == "fynd":
        findings = investigator._findings
        filt = parts[1].upper() if len(parts) > 1 else ""
        if filt in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}:
            findings = [f for f in findings if f.risk_level == filt]
        elif filt:
            findings = [f for f in findings if filt in f.module.upper() or filt in f.finding_id.upper()]
        lines = [f"Totalt {len(findings)} fynd:"]
        for f in findings[:30]:
            note_count = sum(1 for n in investigator.session.investigation_notes if n.get("finding_id") == f.finding_id)
            notes_str = f" [{note_count} noteringar]" if note_count else ""
            lines.append(f"  {f.finding_id:<15} {f.risk_level:<8} {f.module:<22} {f.summary[:60]}{notes_str}")
        if len(findings) > 30:
            lines.append(f"  ... och {len(findings) - 30} till")
        return True, "\n".join(lines)

    if keyword == "notera" and len(parts) >= 3:
        finding_id = parts[1]
        note_text = parts[2]
        investigator.add_note(finding_id, note_text)
        return True, f"Notering sparad för {finding_id}."

    if keyword == "status" and len(parts) >= 3:
        finding_id = parts[1]
        new_status = parts[2].lower()
        investigator.add_note(finding_id, f"Status ändrad till {new_status}", status=new_status)
        return True, f"Status för {finding_id} satt till '{new_status}'."

    if keyword == "bolag":
        lines = [f"Bolag i sfären ({len(investigator._companies)} st):"]
        for c in investigator._companies:
            n_findings = len([f for f in investigator._findings if c.org_nr in f.companies])
            lines.append(f"  {c.name:<30} {c.org_nr}  ({c.role}, {n_findings} fynd)")
        return True, "\n".join(lines)

    if keyword == "historik":
        history = investigator.session.conversation_history
        questions = [m["content"] for m in history if m["role"] == "user"]
        if not questions:
            return True, "Inga frågor ställda i denna session."
        lines = [f"Frågehistorik ({len(questions)} frågor):"]
        for i, q in enumerate(questions, 1):
            lines.append(f"  {i}. {q[:80]}{'...' if len(q) > 80 else ''}")
        return True, "\n".join(lines)

    if keyword == "spara":
        path = session_manager.save_session(investigator.session)
        return True, f"Session sparad: {path}"

    if keyword == "exportera":
        export_path = parts[1] if len(parts) > 1 else str(Path(output_dir) / f"utredning_{investigator.session.session_id}.md")
        investigator.export_investigation(export_path)
        return True, f"Utredningsjournal exporterad: {export_path}"

    if keyword == "avsluta":
        return True, "__AVSLUTA__"

    return False, ""


def _run_followup_mode(args: object, config: dict) -> None:
    """Startar interaktivt uppföljningsläge."""
    import json as _json

    api_key = getattr(args, "api_key", None) or os.environ.get("ANTHROPIC_API_KEY", "")
    no_ai = getattr(args, "no_ai", False)
    output_dir = getattr(args, "output", "./output")
    session_manager = SessionManager(sessions_dir=str(Path(output_dir) / "sessions"))

    # Ladda eller skapa session
    session_path = getattr(args, "session", None)
    if session_path:
        session = session_manager.load_session(session_path)
        results_path = session.results_path
        context_path = session.context_path
        sie_dir = session.source_sie_dir
        csv_dir = session.source_csv_dir
        config_path = session.config_path
    else:
        results_path = getattr(args, "results", None)
        context_path = getattr(args, "context", None)
        sie_dir = args.sie_dir
        csv_dir = args.csv_dir
        config_path = args.config

        if not results_path or not context_path:
            print("Fel: --results och --context krävs för uppföljningsläge (eller --session).")
            sys.exit(1)

        session = session_manager.create_session(
            config_path=config_path,
            results_path=results_path,
            context_path=context_path,
            sie_dir=sie_dir,
            csv_dir=csv_dir,
        )

    # Parsa bolag och bankdata
    companies = _parse_sie_files(sie_dir)
    csv_transactions = _import_csv_files(csv_dir, config)
    bank_tx_by_org = _match_csv_to_companies(companies, csv_transactions, config)

    # Ladda fynd
    findings = _load_findings_from_results(results_path)

    # Läs sfärnamn från context.json
    sphere_name = "Okänd sfär"
    business_description = ""
    try:
        ctx = _json.loads(Path(context_path).read_text(encoding="utf-8"))
        sphere_summary = ctx.get("sphere_summary", {})
        sphere_name = sphere_summary.get("sphere_name", sphere_name)
        business_description = sphere_summary.get("business_description", "")
    except Exception:
        pass

    # Bygg accessor och investigator
    accessor = DataAccessor(companies, bank_tx_by_org, findings)
    investigator = Investigator(
        session=session,
        data_accessor=accessor,
        companies=companies,
        bank_transactions=bank_tx_by_org,
        findings=findings,
        config=config,
        api_key=api_key or "no-key",
        sphere_name=sphere_name,
        business_description=business_description,
    )

    risk_counts: Counter = Counter(f.risk_level for f in findings)

    # Enskild fråga (--question)
    question = getattr(args, "question", None)
    if question:
        if no_ai or not api_key:
            print("Fel: AI-frågor kräver en API-nyckel. Använd inte --no-ai med --question.")
            sys.exit(1)
        answer = investigator.ask(question)
        print(f"\nSVAR:\n{answer}\n")
        return

    # Interaktivt läge
    print("\n" + "╔" + "═" * 54 + "╗")
    print(f"║  Forensisk Bokföringsanalys — Uppföljningsläge      ║")
    print(f"║  Sfär: {sphere_name:<46}║")
    print(f"║  Fynd: {len(findings):<5} totalt ({risk_counts.get('CRITICAL', 0)} kritiska, {risk_counts.get('HIGH', 0)} höga){' ' * 18}║")
    print("╚" + "═" * 54 + "╝")
    print("\nSkriv din fråga (eller 'hjälp' för kommandon, 'avsluta' för att sluta):\n")

    try:
        while True:
            try:
                user_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                user_input = "avsluta"

            if not user_input:
                continue

            # Pröva inbyggda kommandon
            handled, response = _handle_builtin_command(
                user_input, investigator, session_manager, output_dir
            )
            if handled:
                if response == "__AVSLUTA__":
                    break
                print(f"\n{response}\n")
                continue

            # AI-fråga
            if no_ai or not api_key:
                print("\n[AI ej tillgänglig — ingen API-nyckel eller --no-ai angivet]\n")
                continue

            print("\n[Analyserar...]\n")
            answer = investigator.ask(user_input)
            print(f"SVAR:\n{answer}\n")

            # Autospar efter varje fråga
            session_manager.save_session(investigator.session)

    finally:
        saved_path = session_manager.save_session(investigator.session)
        print(f"\nSession sparad: {saved_path}")
        print(f"Fortsätt senare med: python main.py --followup --session {saved_path}")

        export_q = input("\nVill du exportera utredningsjournal? (j/n): ").strip().lower()
        if export_q == "j":
            export_path = str(Path(output_dir) / f"utredning_{sphere_name.replace(' ', '_')}_{investigator.session.session_id}.md")
            investigator.export_investigation(export_path)
            print(f"Exporterad till: {export_path}")


def _run_export_investigation(args: object) -> None:
    """Exportera utredningsjournal från en sessionsfil."""
    import json as _json

    session_path = getattr(args, "export_investigation", None)
    if not session_path:
        print("Fel: --export-investigation kräver en sessionssökväg.")
        sys.exit(1)

    session_manager = SessionManager()
    session = session_manager.load_session(session_path)
    config = _load_config(session.config_path)
    companies = _parse_sie_files(session.source_sie_dir)
    csv_transactions = _import_csv_files(session.source_csv_dir, config)
    bank_tx_by_org = _match_csv_to_companies(companies, csv_transactions, config)
    findings = _load_findings_from_results(session.results_path)

    sphere_name = "Okänd sfär"
    try:
        ctx = _json.loads(Path(session.context_path).read_text(encoding="utf-8"))
        sphere_name = ctx.get("sphere_summary", {}).get("sphere_name", sphere_name)
    except Exception:
        pass

    accessor = DataAccessor(companies, bank_tx_by_org, findings)
    investigator = Investigator(
        session=session,
        data_accessor=accessor,
        companies=companies,
        bank_transactions=bank_tx_by_org,
        findings=findings,
        config=config,
        api_key="no-key",
        sphere_name=sphere_name,
    )

    output_path = getattr(args, "output", f"utredning_{session.session_id}.md")
    if not output_path.endswith(".md"):
        output_path = str(Path(output_path) / f"utredning_{session.session_id}.md")
    investigator.export_investigation(output_path)
    print(f"Exporterad: {output_path}")


# ---------------------------------------------------------------------------
# Huvudfunktion
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forensisk bokföringsanalys av bolagssfär",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="Sökväg till YAML-konfigurationsfil")
    parser.add_argument("--sie-dir", help="Mapp med SIE4-filer (.se/.sie)")
    parser.add_argument("--csv-dir", help="Mapp med bankutdrag (CSV)")
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
    # Steg 13: Uppföljningsläge
    parser.add_argument(
        "--followup",
        action="store_true",
        help="Starta interaktivt uppföljningsläge mot en tidigare analys",
    )
    parser.add_argument("--context", help="Sökväg till context.json från tidigare körning")
    parser.add_argument("--results", help="Sökväg till results.json från tidigare körning")
    parser.add_argument("--session", help="Sökväg till en tidigare session att fortsätta")
    parser.add_argument("-q", "--question", help="Ställ en enskild fråga (annars interaktivt läge)")
    parser.add_argument(
        "--export-investigation",
        metavar="SESSION_PATH",
        help="Exportera utredningsjournal från en sessionsfil",
    )
    args = parser.parse_args()

    # Export-investigation-läge (behöver inte config/sie-dir/csv-dir från args)
    if args.export_investigation:
        _run_export_investigation(args)
        return

    # Validera obligatoriska argument för analys- och uppföljningsläge
    if not args.config:
        parser.error("--config krävs")
    if not args.sie_dir and not args.session:
        parser.error("--sie-dir krävs (eller --session för att återuppta session)")
    if not args.csv_dir and not args.session:
        parser.error("--csv-dir krävs (eller --session för att återuppta session)")

    # 1. Ladda config
    logger.info("Laddar konfiguration: %s", args.config)
    config = _load_config(args.config)

    # Uppföljningsläge
    if args.followup or args.session or args.question:
        _run_followup_mode(args, config)
        return

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
