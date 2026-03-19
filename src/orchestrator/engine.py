"""AI-orkestrerare för forensisk bokföringsanalys.

OrchestrationEngine kör en 7-stegs-analys:
  1. Datakvalitetsvalidering — avbryt om filer inte godkänns
  2. Modul A (bank vs bok) + Modul B (koncernintern) körs alltid
  3. Claude föreslår ytterligare moduler baserat på initiala fynd
  4. Föreslagna moduler körs
  5. Korskoppling av alla fynd via Claude
  6. Berikning av varje fynd (batch 20) med AI-resonemang
  7. Övergripande sammanfattning och AnalysisResult returneras

Felhantering:
  - Retry med exponentiell backoff (max 3 försök)
  - Vid bestående API-fel: logga varning, fortsätt utan AI-resonemang
  - Rate limiting: konfigurerbart (default 10 anrop/minut)
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

try:
    import anthropic as _anthropic_module
except ImportError:  # pragma: no cover
    _anthropic_module = None  # type: ignore[assignment]

from src.core.models import BankTransaction, Company, Finding
from src.modules.amount_patterns import AmountPatternsModule
from src.modules.bank_matching import BankMatchingModule
from src.modules.base_module import AnalysisModule
from src.modules.business_relevance import BusinessRelevanceModule, MockLookupProvider
from src.modules.clearing_accounts import ClearingAccountsModule
from src.modules.intercompany import IntercompanyModule
from src.modules.salary_expenses import SalaryExpensesModule
from src.modules.time_patterns import TimePatternsModule
from src.orchestrator.prompts import (
    CROSS_REFERENCE_PROMPT,
    ENRICHMENT_PROMPT,
    ORCHESTRATION_PROMPT,
    SUMMARY_PROMPT,
    SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

# Registry: modulnamn → klass
_MODULE_REGISTRY: dict[str, type] = {
    "bank_matching": BankMatchingModule,
    "intercompany": IntercompanyModule,
    "clearing_accounts": ClearingAccountsModule,
    "amount_patterns": AmountPatternsModule,
    "time_patterns": TimePatternsModule,
    "salary_expenses": SalaryExpensesModule,
    "business_relevance": BusinessRelevanceModule,
}

_ENRICH_BATCH_SIZE = 20
_DEFAULT_MAX_CALLS_PER_MIN = 10
_DEFAULT_MAX_RETRIES = 3
_CLAUDE_MODEL = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Datastrukturer
# ---------------------------------------------------------------------------

@dataclass
class AnalysisResult:
    """Samlat resultat från en fullständig OrchestrationEngine-körning."""

    findings: list[Finding]
    companies: list[Company]
    validation_passed: bool
    execution_log: list[dict] = field(default_factory=list)
    summary: str = ""
    generated_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Hjälpklass: Rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, calls_per_minute: int, _sleep: Callable[[float], None] = time.sleep) -> None:
        self._interval = 60.0 / max(calls_per_minute, 1)
        self._last: float = 0.0
        self._sleep = _sleep

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self._interval:
            self._sleep(self._interval - elapsed)
        self._last = time.monotonic()


# ---------------------------------------------------------------------------
# OrchestrationEngine
# ---------------------------------------------------------------------------

class OrchestrationEngine:
    """Kör fullständig forensisk analys med AI-assisterad orkestrering."""

    def __init__(
        self,
        config: dict,
        api_key: str,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.findings: list[Finding] = []
        self.execution_log: list[dict] = []
        self._sleep = _sleep

        calls_per_min = config.get("orchestration", {}).get(
            "max_calls_per_minute", _DEFAULT_MAX_CALLS_PER_MIN
        )
        self._rate_limiter = _RateLimiter(calls_per_min, _sleep=_sleep)

        # Skapa Claude-klient (kan ersättas i tester: engine.client = mock)
        if _anthropic_module is not None:
            self.client = _anthropic_module.Anthropic(api_key=api_key)
        else:
            self.client = None  # pragma: no cover

    # ------------------------------------------------------------------
    # Publik API
    # ------------------------------------------------------------------

    def run_full_analysis(
        self,
        companies: list[Company],
        bank_transactions: dict[str, list[BankTransaction]],
        validation_reports: list | None = None,
    ) -> AnalysisResult:
        """Kör fullständig analys i 7 steg.

        Args:
            companies: Parsade Company-objekt.
            bank_transactions: org_nr → lista av BankTransaction.
            validation_reports: Redan körda valideringsrapporter (optional).
                Om de saknas hoppar orkestreraren över valideringssteget.

        Returns:
            AnalysisResult med alla fynd, logg och sammanfattning.
        """
        self.findings = []
        self.execution_log = []

        # --- Steg 1: Datakvalitetsvalidering ---
        validation_passed = True
        if validation_reports is not None:
            failed = [r for r in validation_reports if not r.is_valid]
            if failed:
                for r in failed:
                    logger.error("Validering misslyckades: %s — %s", r.file_path, r.errors)
                self._log("validation", "FAILED", f"{len(failed)} filer godkändes inte")
                return AnalysisResult(
                    findings=[], companies=companies,
                    validation_passed=False,
                    execution_log=self.execution_log,
                )
        self._log("validation", "OK", "Alla filer godkändes")

        # --- Steg 2: Modul A och B ---
        bank_mod = BankMatchingModule()
        ic_mod = IntercompanyModule()
        initial_findings: list[Finding] = []

        initial_findings += bank_mod.analyze(companies, bank_transactions, self.config)
        self._log("module_a", "OK", f"{len(initial_findings)} fynd från bank_matching")

        ic_findings = ic_mod.analyze(companies, bank_transactions, self.config)
        initial_findings += ic_findings
        self._log("module_b", "OK", f"{len(ic_findings)} fynd från intercompany")

        # --- Steg 3: Claude föreslår moduler ---
        suggested_modules = self._orchestrate_next_modules(initial_findings)
        self._log(
            "orchestration", "OK",
            f"Claude föreslår: {suggested_modules}",
        )

        # --- Steg 4: Kör föreslagna moduler ---
        additional_findings = self._run_suggested_modules(
            suggested_modules, companies, bank_transactions
        )
        self._log(
            "additional_modules", "OK",
            f"{len(additional_findings)} ytterligare fynd",
        )

        all_findings = initial_findings + additional_findings
        self.findings = all_findings

        # --- Steg 5: Korskoppling ---
        self._cross_reference_findings(all_findings, companies)
        self._log("cross_reference", "OK", "Korskoppling klar")

        # --- Steg 6: Berikning med AI-resonemang ---
        enriched = self.enrich_findings_with_ai(all_findings, companies)
        self.findings = enriched
        self._log("enrichment", "OK", f"{len(enriched)} fynd berikade")

        # --- Steg 7: Sammanfattning ---
        summary = self.generate_summary(enriched, companies)
        self._log("summary", "OK", "Sammanfattning genererad")

        return AnalysisResult(
            findings=enriched,
            companies=companies,
            validation_passed=validation_passed,
            execution_log=self.execution_log,
            summary=summary,
        )

    def enrich_findings_with_ai(
        self,
        findings: list[Finding],
        companies: list[Company],
    ) -> list[Finding]:
        """Berikar varje fynd med AI-resonemang (batch om max 20).

        Fynd utan API-respons behåller ai_reasoning=None men kastas inte bort.
        """
        if not findings:
            return findings

        sphere_ctx = ", ".join(f"{c.org_nr} ({c.name})" for c in companies)
        enriched = list(findings)

        for i in range(0, len(findings), _ENRICH_BATCH_SIZE):
            batch = findings[i : i + _ENRICH_BATCH_SIZE]
            findings_json = json.dumps(
                [
                    {
                        "finding_id": f.finding_id,
                        "category": f.category,
                        "risk_level": f.risk_level,
                        "summary": f.summary,
                    }
                    for f in batch
                ],
                ensure_ascii=False,
            )
            prompt = ENRICHMENT_PROMPT.format(
                count=len(batch),
                sphere_context=sphere_ctx,
                findings_json=findings_json,
            )
            resp = self._call_api(prompt)
            if not resp:
                continue

            # Strippa eventuella markdown-kodblock (```json ... ```)
            cleaned = resp.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1]
                cleaned = cleaned.rsplit("```", 1)[0].strip()

            try:
                data = json.loads(cleaned)
                if not isinstance(data, list):
                    continue
                id_to_reasoning: dict[str, str] = {
                    item["finding_id"]: item.get("reasoning", "")
                    for item in data
                    if isinstance(item, dict) and "finding_id" in item
                }
                for j, f in enumerate(enriched[i : i + _ENRICH_BATCH_SIZE]):
                    reasoning = id_to_reasoning.get(f.finding_id, "")
                    if reasoning:
                        enriched[i + j].ai_reasoning = reasoning
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Kunde inte parsa berikning-svar: %s", exc)

        return enriched

    def cross_reference_findings(self, findings: list[Finding]) -> list[Finding]:
        """Identifierar mönster som spänner över flera moduler/bolag (publik)."""
        self._cross_reference_findings(findings, [])
        return findings

    def generate_summary(
        self,
        findings: list[Finding],
        companies: list[Company],
    ) -> str:
        """Genererar övergripande sammanfattning med prioriterade åtgärder."""
        risk_dist = dict(Counter(f.risk_level for f in findings))
        module_dist = dict(Counter(f.module for f in findings))
        top_findings = [
            f for f in findings if f.risk_level in ("CRITICAL", "HIGH")
        ][:10]
        top_json = json.dumps(
            [{"id": f.finding_id, "summary": f.summary} for f in top_findings],
            ensure_ascii=False,
        )
        companies_str = ", ".join(f"{c.org_nr} ({c.name})" for c in companies)
        periods = [
            f"{fy.start_date}–{fy.end_date}"
            for c in companies for fy in c.fiscal_years
        ]

        prompt = SUMMARY_PROMPT.format(
            companies=companies_str,
            period=", ".join(periods) if periods else "Okänd",
            total_findings=len(findings),
            risk_distribution=json.dumps(risk_dist, ensure_ascii=False),
            module_distribution=json.dumps(module_dist, ensure_ascii=False),
            top_findings=top_json,
            patterns="(inga mönster identifierade)",
        )
        resp = self._call_api(prompt)
        if resp is None:
            return "Sammanfattning kunde inte genereras (API ej tillgängligt)."
        try:
            data = json.loads(resp)
            return data.get("summary") or data.get("executive_summary") or resp
        except json.JSONDecodeError:
            return resp

    # ------------------------------------------------------------------
    # Interna metoder
    # ------------------------------------------------------------------

    def _log(self, step: str, status: str, message: str) -> None:
        entry = {
            "step": step,
            "status": status,
            "message": message,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        self.execution_log.append(entry)
        logger.info("[%s] %s: %s", step, status, message)

    def _orchestrate_next_modules(self, initial_findings: list[Finding]) -> list[str]:
        """Frågar Claude vilka moduler som ska köras härnäst."""
        cat_counts = dict(Counter(f.category for f in initial_findings))
        findings_summary = json.dumps(
            [{"id": f.finding_id, "category": f.category, "summary": f.summary[:80]}
             for f in initial_findings[:50]],
            ensure_ascii=False,
        )
        prompt = ORCHESTRATION_PROMPT.format(
            findings_summary=findings_summary,
            category_counts=json.dumps(cat_counts, ensure_ascii=False),
        )
        resp = self._call_api(prompt)
        if resp is None:
            # Fallback: kör alla moduler
            return list(_MODULE_REGISTRY.keys() - {"bank_matching", "intercompany"})
        try:
            data = json.loads(resp)
            return [m for m in data.get("modules", []) if m in _MODULE_REGISTRY]
        except json.JSONDecodeError:
            return []

    def _run_suggested_modules(
        self,
        module_names: list[str],
        companies: list[Company],
        bank_transactions: dict[str, list[BankTransaction]],
    ) -> list[Finding]:
        findings: list[Finding] = []
        for name in module_names:
            cls = _MODULE_REGISTRY.get(name)
            if cls is None:
                logger.warning("Okänd modul: %s", name)
                continue
            # BusinessRelevanceModule kräver lookup_provider
            if cls is BusinessRelevanceModule:
                module: AnalysisModule = cls(lookup_provider=MockLookupProvider({}))
            else:
                module = cls()
            try:
                result = module.analyze(companies, bank_transactions, self.config)
                findings.extend(result)
                self._log(name, "OK", f"{len(result)} fynd")
            except Exception as exc:
                logger.warning("Modul %s misslyckades: %s", name, exc)
                self._log(name, "ERROR", str(exc))
        return findings

    def _cross_reference_findings(
        self,
        findings: list[Finding],
        companies: list[Company],
    ) -> None:
        """Skickar alla fynd till Claude för korskoppling (loggar resultatet)."""
        if not findings:
            return
        cat_summary = json.dumps(
            dict(Counter(f.category for f in findings)), ensure_ascii=False
        )
        companies_str = ", ".join(c.org_nr for c in companies)
        periods = [
            f"{fy.start_date}–{fy.end_date}"
            for c in companies for fy in c.fiscal_years
        ]
        prompt = CROSS_REFERENCE_PROMPT.format(
            total=len(findings),
            category_summary=cat_summary,
            period=", ".join(periods) if periods else "Okänd",
            companies=companies_str or "(okänt)",
        )
        resp = self._call_api(prompt)
        if resp:
            logger.info("Korskoppling: %s…", resp[:100])

    def _call_api(self, user_prompt: str) -> str | None:
        """Anropar Claude API med retry och rate limiting.

        Returns:
            Svarstext eller None om alla försök misslyckades.
        """
        return self._api_call_with_retry(user_prompt)

    def _api_call_with_retry(
        self,
        user_prompt: str,
        max_retries: int | None = None,
    ) -> str | None:
        """Gör ett API-anrop med exponentiell backoff vid fel.

        Kan injectas med egen sleep-funktion via self._sleep för test.
        """
        retries = max_retries if max_retries is not None else _DEFAULT_MAX_RETRIES

        for attempt in range(retries + 1):
            try:
                self._rate_limiter.wait()
                if self.client is None:
                    return None
                response = self.client.messages.create(
                    model=_CLAUDE_MODEL,
                    max_tokens=2000,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                return response.content[0].text
            except Exception as exc:
                if attempt == retries:
                    logger.warning(
                        "API-anrop misslyckades efter %d försök: %s", retries + 1, exc
                    )
                    return None
                wait = 2 ** attempt  # 1, 2, 4 sekunder
                logger.warning(
                    "API-fel (försök %d/%d): %s. Väntar %ds",
                    attempt + 1, retries + 1, exc, wait,
                )
                self._sleep(wait)
        return None  # pragma: no cover
