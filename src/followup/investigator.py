"""Uppföljningsmotor för interaktiva granskningsfrågor.

Hanterar kommunikation med Claude API via tool use, sparar sessionshistorik
och exporterar utredningsjournaler.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import anthropic

from src.core.models import BankTransaction, Company, Finding
from src.followup.data_accessor import DataAccessor
from src.followup.prompts import INVESTIGATION_TOOL_DEFINITIONS, INVESTIGATOR_SYSTEM_PROMPT
from src.followup.session import InvestigationSession
from src.modules.amount_patterns import AmountPatternsModule
from src.modules.bank_matching import BankMatchingModule
from src.modules.business_relevance import BusinessRelevanceModule, MockLookupProvider
from src.modules.clearing_accounts import ClearingAccountsModule
from src.modules.intercompany import IntercompanyModule
from src.modules.salary_expenses import SalaryExpensesModule
from src.modules.time_patterns import TimePatternsModule

logger = logging.getLogger(__name__)

_RISK_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
_VALID_STATUSES = {"open", "investigated", "confirmed", "dismissed", "escalated"}
_MODULE_REGISTRY = {
    "bank_matching": BankMatchingModule,
    "intercompany": IntercompanyModule,
    "clearing_accounts": ClearingAccountsModule,
    "amount_patterns": AmountPatternsModule,
    "time_patterns": TimePatternsModule,
    "salary_expenses": SalaryExpensesModule,
}


class Investigator:
    """Hanterar interaktiva uppföljningsfrågor mot analysresultat."""

    def __init__(
        self,
        session: InvestigationSession,
        data_accessor: DataAccessor,
        companies: list[Company],
        bank_transactions: dict[str, list[BankTransaction]],
        findings: list[Finding],
        config: dict,
        api_key: str,
        sphere_name: str = "Okänd sfär",
        business_description: str = "",
    ) -> None:
        self.session = session
        self.data = data_accessor
        self._companies = companies
        self._bank_tx = bank_transactions
        self._findings = findings
        self.config = config
        self.sphere_name = sphere_name
        self.business_description = business_description
        self.client = anthropic.Anthropic(api_key=api_key)

    # ------------------------------------------------------------------
    # Publik API
    # ------------------------------------------------------------------

    def ask(self, question: str) -> str:
        """Ställ en uppföljningsfråga mot analysresultaten.

        Stödjer Claude tool use för datahämtning. Sparar fråga/svar
        i sessionshistoriken.
        """
        self.session.conversation_history.append({"role": "user", "content": question})
        answer = self._run_conversation(question)
        self.session.conversation_history.append({"role": "assistant", "content": answer})
        return answer

    def add_note(self, finding_id: str, note: str, status: str | None = None) -> None:
        """Lägg till revisorns anteckning om ett fynd."""
        entry: dict = {
            "finding_id": finding_id,
            "note": note,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        if status and status in _VALID_STATUSES:
            entry["status"] = status
            # Uppdatera fyndets status om det finns i listan
            for f in self._findings:
                if f.finding_id == finding_id:
                    f.status = status
                    break
        self.session.investigation_notes.append(entry)

    def rerun_module(self, module_name: str, params: dict) -> list[Finding]:
        """Kör om en specifik modul med nya parametrar."""
        module_cls = _MODULE_REGISTRY.get(module_name)
        if not module_cls:
            logger.warning("Okänd modul: %s", module_name)
            return []

        # Filtrera bolag om company_filter anges
        company_filter = params.get("company_filter")
        companies = self._companies
        if company_filter:
            companies = [
                c for c in self._companies
                if c.org_nr == company_filter or c.name.lower() == company_filter.lower()
            ]

        # Filtrera bank_tx till berörda bolag
        bank_tx = {org: txs for org, txs in self._bank_tx.items()
                   if not company_filter or org in {c.org_nr for c in companies}}

        module = module_cls()
        findings = module.analyze(companies, bank_tx, self.config)

        self.session.rerun_log.append({
            "module": module_name,
            "params": params,
            "findings_count": len(findings),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        })
        return findings

    def export_investigation(self, output_path: str) -> None:
        """Exportera hela utredningen som Markdown-journal."""
        lines: list[str] = []
        lines.append(f"# Utredningsjournal: {self.sphere_name}")
        lines.append(f"**Analysdatum:** {self.session.started_at.strftime('%Y-%m-%d')}")
        lines.append(f"**Session:** {self.session.session_id}")
        lines.append("")

        # Sammanfattning
        by_risk: Counter = Counter(f.risk_level for f in self._findings)
        notes_count = len(self.session.investigation_notes)
        questions = len([m for m in self.session.conversation_history if m["role"] == "user"])
        confirmed = sum(1 for n in self.session.investigation_notes if n.get("status") == "confirmed")
        escalated = sum(1 for n in self.session.investigation_notes if n.get("status") == "escalated")

        lines.append("## Sammanfattning")
        risk_parts = ", ".join(
            f"{by_risk.get(r, 0)} {r.lower()}" for r in _RISK_ORDER if by_risk.get(r, 0)
        )
        lines.append(f"- Totalt {len(self._findings)} fynd: {risk_parts}")
        lines.append(f"- {confirmed} fynd markerade som bekräftade")
        lines.append(f"- {escalated} fynd eskalerade")
        lines.append(f"- {questions} uppföljningsfrågor ställda")
        lines.append("")

        # Frågor och svar
        if self.session.conversation_history:
            lines.append("## Frågor och svar")
            history = self.session.conversation_history
            q_num = 0
            for i, msg in enumerate(history):
                if msg["role"] == "user":
                    q_num += 1
                    ts = ""
                    lines.append(f"\n### Fråga {q_num}")
                    lines.append(f"**Fråga:** {msg['content']}")
                    # Nästa meddelande är svaret
                    if i + 1 < len(history) and history[i + 1]["role"] == "assistant":
                        lines.append(f"\n**Svar:** {history[i + 1]['content']}")
                    lines.append("\n---")
            lines.append("")

        # Noteringar
        if self.session.investigation_notes:
            lines.append("## Noteringar")
            lines.append("")
            lines.append("| Fynd | Status | Notering |")
            lines.append("|------|--------|----------|")
            for note in self.session.investigation_notes:
                fid = note.get("finding_id", "?")
                status = note.get("status", "—")
                text = note.get("note", "").replace("|", "\\|")
                lines.append(f"| {fid} | {status} | {text} |")
            lines.append("")

        # Moduler som kördes om
        lines.append("## Moduler som kördes om")
        if self.session.rerun_log:
            for entry in self.session.rerun_log:
                lines.append(
                    f"- {entry['timestamp']}: {entry['module']} "
                    f"(params: {json.dumps(entry['params'], ensure_ascii=False)}, "
                    f"fynd: {entry['findings_count']})"
                )
        else:
            lines.append("Inga moduler kördes om under denna session.")

        Path(output_path).write_text("\n".join(lines), encoding="utf-8")
        logger.info("Utredningsjournal exporterad: %s", output_path)

    # ------------------------------------------------------------------
    # Intern Claude-konversation med tool use
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        risk_counts: Counter = Counter(f.risk_level for f in self._findings)
        company_list = "\n".join(
            f"  - {c.name} ({c.org_nr}, {c.role})" for c in self._companies
        )
        tool_names = [t["name"] for t in INVESTIGATION_TOOL_DEFINITIONS]
        tool_desc = ", ".join(tool_names)

        prev_questions = [
            m["content"] for m in self.session.conversation_history if m["role"] == "user"
        ]
        conv_summary = (
            "\n".join(f"  - {q}" for q in prev_questions[-5:])
            if prev_questions
            else "  (Inga tidigare frågor)"
        )

        return INVESTIGATOR_SYSTEM_PROMPT.format(
            sphere_name=self.sphere_name,
            business_description=self.business_description or "Ej angiven",
            company_list=company_list,
            total_findings=len(self._findings),
            critical_count=risk_counts.get("CRITICAL", 0),
            high_count=risk_counts.get("HIGH", 0),
            tool_descriptions=tool_desc,
            conversation_summary=conv_summary,
        )

    def _run_conversation(self, question: str) -> str:
        """Kör ett Claude-samtal med tool use-stöd."""
        system = self._build_system_prompt()
        messages = [{"role": "user", "content": question}]

        max_rounds = 5  # säkerhetsbrytare mot oändliga tool-loopar
        for _ in range(max_rounds):
            try:
                response = self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=4096,
                    system=system,
                    tools=INVESTIGATION_TOOL_DEFINITIONS,
                    messages=messages,
                )
            except Exception as exc:
                logger.warning("API-anrop misslyckades: %s", exc)
                return f"[Fel: kunde inte nå Claude API — {exc}]"

            # Bygg assistent-meddelandet
            assistant_message: dict = {"role": "assistant", "content": response.content}
            messages.append(assistant_message)

            if response.stop_reason == "end_turn":
                # Extrahera textsvaret
                for block in response.content:
                    if hasattr(block, "text"):
                        return block.text
                return ""

            if response.stop_reason != "tool_use":
                break

            # Hantera tool_use-anrop
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_result = self._dispatch_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                })

            messages.append({"role": "user", "content": tool_results})

        return "[Svar kunde inte genereras — max antal verktygsanrop nått]"

    def _dispatch_tool(self, tool_name: str, inputs: dict) -> object:
        """Anropar rätt DataAccessor-metod baserat på verktygsnamn."""
        try:
            if tool_name == "get_transactions":
                return self._tool_get_transactions(inputs)
            if tool_name == "get_monthly_totals":
                return self._tool_get_monthly_totals(inputs)
            if tool_name == "trace_money":
                return self._tool_trace_money(inputs)
            if tool_name == "compare_companies":
                return self.data.compare_accounts_across_companies(inputs["account"])
            if tool_name == "get_findings":
                return self._tool_get_findings(inputs)
            if tool_name == "get_intercompany_matrix":
                return self.data.get_intercompany_matrix()
            if tool_name == "rerun_module":
                findings = self.rerun_module(inputs["module"], inputs)
                return [
                    {"finding_id": f.finding_id, "risk": f.risk_level,
                     "summary": f.summary, "category": f.category}
                    for f in findings
                ]
        except Exception as exc:
            logger.warning("Verktygsanrop %s misslyckades: %s", tool_name, exc)
            return {"error": str(exc)}
        return {"error": f"Okänt verktyg: {tool_name}"}

    def _tool_get_transactions(self, inputs: dict) -> list[dict]:
        company_str = inputs.get("company") or ""
        org_nr = self.data._resolve_org(company_str) if company_str else None
        text_search = inputs.get("text_search") or inputs.get("counterparty") or ""
        account = inputs.get("account")
        amount_min = inputs.get("amount_min_kr")
        amount_max = inputs.get("amount_max_kr")
        start_d = _parse_date(inputs.get("start_date"))
        end_d = _parse_date(inputs.get("end_date"))

        if text_search:
            txs = self.data.get_transactions_by_text(text_search, org_nr)
        elif account and org_nr:
            txs = self.data.get_transactions_by_account(org_nr, account, start_d, end_d)
        elif amount_min is not None and amount_max is not None:
            txs = self.data.get_transactions_by_amount(amount_min, amount_max, org_nr)
        else:
            # Hämta alla transaktioner (filtrerat på bolag om angivet)
            txs = self.data.get_transaction_timeline(
                company_org_nr=org_nr,
                account=account,
            )

        # Tillämpa datumfilter på resultatet
        if start_d or end_d:
            txs = [
                t for t in txs
                if (not start_d or t["date"] >= str(start_d))
                and (not end_d or t["date"] <= str(end_d))
            ]
        return txs[:200]  # cap för promptstorlek

    def _tool_get_monthly_totals(self, inputs: dict) -> list[dict]:
        company_str = inputs.get("company", "")
        org_nr = self.data._resolve_org(company_str) or company_str
        return self.data.get_monthly_totals(org_nr, inputs["account"])

    def _tool_trace_money(self, inputs: dict) -> list[dict]:
        return self.data.trace_intercompany_flow(
            amount_kr=inputs["amount_kr"],
            tolerance_kr=inputs.get("tolerance_kr", 500),
            start_date=_parse_date(inputs.get("start_date")),
            end_date=_parse_date(inputs.get("end_date")),
        )

    def _tool_get_findings(self, inputs: dict) -> list[dict]:
        finding_ids = inputs.get("finding_ids") or []
        company = inputs.get("company")
        module = inputs.get("module")
        risk = inputs.get("risk_level")

        if finding_ids:
            findings = self.data.get_findings_by_id(finding_ids)
        elif company:
            org_nr = self.data._resolve_org(company) or company
            findings = self.data.get_findings_by_company(org_nr)
        elif module:
            findings = self.data.get_findings_by_module(module)
        elif risk:
            findings = self.data.get_findings_by_risk(risk)
        else:
            findings = self._findings

        return [
            {
                "finding_id": f.finding_id,
                "module": f.module,
                "companies": f.companies,
                "risk_level": f.risk_level,
                "category": f.category,
                "summary": f.summary,
                "ai_reasoning": f.ai_reasoning,
                "status": f.status,
            }
            for f in findings[:50]  # cap
        ]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
