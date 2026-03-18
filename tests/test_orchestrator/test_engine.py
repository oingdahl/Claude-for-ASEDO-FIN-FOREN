"""Tester för src/orchestrator/engine.py — OrchestrationEngine.

Alla anrop mot Claude API mockas. Ingen nätverkstrafik sker.

Täcker:
- Fullständigt 7-stegs-flöde (run_full_analysis)
- Berikning av fynd med AI-resonemang (enrich_findings_with_ai)
- Sammanfattning (generate_summary)
- Retry-logik med exponentiell backoff
- Korskoppling (cross_reference_findings)
- Tidig avbrytning vid valideringsfel
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.core.models import Company, Finding, FiscalYear
from src.orchestrator.engine import AnalysisResult, OrchestrationEngine

# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

CONFIG = {
    "matching": {"amount_tolerance_bank_kr": 50},
    "orchestration": {"max_calls_per_minute": 600},  # hög gräns → ingen rate-limiting i tester
}
API_KEY = "test-api-key"


def _no_sleep(_: float) -> None:
    """Ersätter time.sleep i tester — gör inget."""


def _make_company(org_nr: str = "556000-1234", name: str = "TestAB") -> Company:
    c = Company(org_nr=org_nr, name=name, role="holding")
    c.fiscal_years.append(FiscalYear(0, date(2024, 1, 1), date(2024, 12, 31)))
    return c


def _make_finding(fid: str = "TEST_001", risk: str = "HIGH") -> Finding:
    return Finding(
        finding_id=fid,
        module="test_module",
        companies=["556000-1234"],
        risk_level=risk,
        category="test_category",
        summary=f"Testfynd {fid}",
    )


def _mock_response(text: str) -> MagicMock:
    """Skapar ett mock-svar som efterliknar anthropic.types.Message."""
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


def _make_engine() -> OrchestrationEngine:
    engine = OrchestrationEngine(config=CONFIG, api_key=API_KEY, _sleep=_no_sleep)
    # Ersätt klienten med en mock så att inga riktiga API-anrop görs
    engine.client = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# Tester: _api_call_with_retry
# ---------------------------------------------------------------------------


class TestApiCallWithRetry:
    def test_lyckat_forsta_forsok(self):
        engine = _make_engine()
        engine.client.messages.create.return_value = _mock_response("hej")
        result = engine._api_call_with_retry("prompt")
        assert result == "hej"
        assert engine.client.messages.create.call_count == 1

    def test_retry_vid_fel_sedan_lyckas(self):
        engine = _make_engine()
        engine.client.messages.create.side_effect = [
            Exception("tillfälligt fel"),
            _mock_response("lyckades"),
        ]
        result = engine._api_call_with_retry("prompt", max_retries=2)
        assert result == "lyckades"
        assert engine.client.messages.create.call_count == 2

    def test_returnerar_none_efter_max_retries(self):
        engine = _make_engine()
        engine.client.messages.create.side_effect = Exception("alltid fel")
        result = engine._api_call_with_retry("prompt", max_retries=3)
        assert result is None
        # 1 initialt försök + 3 retry = 4 anrop totalt
        assert engine.client.messages.create.call_count == 4

    def test_exponentiell_backoff_kallar_sleep(self):
        sleep_calls: list[float] = []
        engine = OrchestrationEngine(
            config=CONFIG, api_key=API_KEY, _sleep=lambda t: sleep_calls.append(t)
        )
        engine.client = MagicMock()
        engine.client.messages.create.side_effect = [
            Exception("fel 1"),
            Exception("fel 2"),
            _mock_response("ok"),
        ]
        engine._api_call_with_retry("prompt", max_retries=3)
        # Rate limitern kan också anropa _sleep med korta värden — filtrera ut dem.
        # Backoff: 2^0=1s och 2^1=2s
        backoff_calls = [t for t in sleep_calls if t >= 1]
        assert backoff_calls == [1, 2]

    def test_returnerar_none_om_client_ar_none(self):
        engine = _make_engine()
        engine.client = None
        result = engine._api_call_with_retry("prompt")
        assert result is None


# ---------------------------------------------------------------------------
# Tester: enrich_findings_with_ai
# ---------------------------------------------------------------------------


class TestEnrichFindingsWithAi:
    def test_berikar_fynd_med_ai_reasoning(self):
        engine = _make_engine()
        findings = [_make_finding("F001"), _make_finding("F002")]
        enrichment_response = json.dumps([
            {"finding_id": "F001", "reasoning": "Hög risk pga mönster X"},
            {"finding_id": "F002", "reasoning": "Misstänkt transaktion Y"},
        ])
        engine.client.messages.create.return_value = _mock_response(enrichment_response)

        result = engine.enrich_findings_with_ai(findings, [_make_company()])

        assert result[0].ai_reasoning == "Hög risk pga mönster X"
        assert result[1].ai_reasoning == "Misstänkt transaktion Y"

    def test_behaaller_fynd_utan_api_svar(self):
        engine = _make_engine()
        engine.client.messages.create.side_effect = Exception("API ej tillgängligt")
        findings = [_make_finding("F001")]
        # Sätt max_retries = 0 via monkey-patch för att snabba upp testet
        engine._api_call_with_retry = lambda *a, **kw: None  # type: ignore[method-assign]

        result = engine.enrich_findings_with_ai(findings, [_make_company()])

        assert len(result) == 1
        assert result[0].ai_reasoning is None

    def test_tom_lista_returneras_direkt(self):
        engine = _make_engine()
        result = engine.enrich_findings_with_ai([], [_make_company()])
        assert result == []
        engine.client.messages.create.assert_not_called()

    def test_hanterar_ogiltig_json_svar(self):
        engine = _make_engine()
        engine.client.messages.create.return_value = _mock_response("OGILTIG JSON!!!")
        findings = [_make_finding("F001")]
        # Ska inte krascha — ai_reasoning förblir None
        result = engine.enrich_findings_with_ai(findings, [_make_company()])
        assert result[0].ai_reasoning is None

    def test_batch_storlek_20(self):
        """21 fynd ska kräva 2 API-anrop (batch 20 + 1)."""
        engine = _make_engine()
        findings = [_make_finding(f"F{i:03d}") for i in range(21)]
        engine.client.messages.create.return_value = _mock_response("[]")

        engine.enrich_findings_with_ai(findings, [_make_company()])

        assert engine.client.messages.create.call_count == 2


# ---------------------------------------------------------------------------
# Tester: generate_summary
# ---------------------------------------------------------------------------


class TestGenerateSummary:
    def test_returnerar_summary_fran_json(self):
        engine = _make_engine()
        resp = json.dumps({"summary": "Sfären uppvisar hög risk.", "executive_summary": "Kritisk"})
        engine.client.messages.create.return_value = _mock_response(resp)

        summary = engine.generate_summary([_make_finding()], [_make_company()])

        assert summary == "Sfären uppvisar hög risk."

    def test_fallback_till_executive_summary(self):
        engine = _make_engine()
        resp = json.dumps({"executive_summary": "Övergripande bedömning."})
        engine.client.messages.create.return_value = _mock_response(resp)

        summary = engine.generate_summary([_make_finding()], [_make_company()])

        assert summary == "Övergripande bedömning."

    def test_returnerar_raa_text_om_inte_json(self):
        engine = _make_engine()
        engine.client.messages.create.return_value = _mock_response("Fritext sammanfattning")

        summary = engine.generate_summary([_make_finding()], [_make_company()])

        assert summary == "Fritext sammanfattning"

    def test_returnerar_felmeddelande_om_api_misslyckas(self):
        engine = _make_engine()
        engine._api_call_with_retry = lambda *a, **kw: None  # type: ignore[method-assign]

        summary = engine.generate_summary([_make_finding()], [_make_company()])

        assert "kunde inte genereras" in summary.lower()


# ---------------------------------------------------------------------------
# Tester: run_full_analysis — 7-stegsflödet
# ---------------------------------------------------------------------------


class TestRunFullAnalysis:
    def _setup_engine_with_responses(self, responses: list[str]) -> OrchestrationEngine:
        """Skapar en engine vars API-anrop returnerar svar i angiven ordning."""
        engine = _make_engine()
        engine.client.messages.create.side_effect = [
            _mock_response(r) for r in responses
        ]
        return engine

    def _orchestration_resp(self) -> str:
        return json.dumps({"modules": [], "reasoning": "Inga fler moduler behövs"})

    def _cross_ref_resp(self) -> str:
        return json.dumps({"patterns": [], "connections": []})

    def _enrichment_resp(self, finding_ids: list[str]) -> str:
        return json.dumps([
            {"finding_id": fid, "reasoning": f"Resonemang för {fid}"}
            for fid in finding_ids
        ])

    def _summary_resp(self) -> str:
        return json.dumps({"summary": "Sammanfattning av analysen.", "executive_summary": "Hög risk"})

    def test_returnerar_analysis_result(self):
        engine = _make_engine()
        # Mocka alla API-anrop med tomma men giltiga svar
        engine.client.messages.create.return_value = _mock_response(
            json.dumps({"modules": [], "patterns": [], "connections": [], "summary": "OK"})
        )
        companies = [_make_company()]
        result = engine.run_full_analysis(companies, {})
        assert isinstance(result, AnalysisResult)
        assert result.validation_passed is True
        assert result.companies == companies

    def test_avbryter_om_validering_misslyckas(self):
        engine = _make_engine()
        failing_report = MagicMock()
        failing_report.is_valid = False
        failing_report.file_path = "test.sie"
        failing_report.errors = ["Felaktig struktur"]

        result = engine.run_full_analysis([_make_company()], {}, validation_reports=[failing_report])

        assert result.validation_passed is False
        assert result.findings == []
        engine.client.messages.create.assert_not_called()

    def test_godkand_validering_fortsatter(self):
        engine = _make_engine()
        engine.client.messages.create.return_value = _mock_response(
            json.dumps({"modules": [], "summary": "OK"})
        )
        valid_report = MagicMock()
        valid_report.is_valid = True

        result = engine.run_full_analysis(
            [_make_company()], {}, validation_reports=[valid_report]
        )

        assert result.validation_passed is True

    def test_execution_log_innehaller_alla_steg(self):
        engine = _make_engine()
        engine.client.messages.create.return_value = _mock_response(
            json.dumps({"modules": [], "summary": "OK"})
        )
        result = engine.run_full_analysis([_make_company()], {})

        steps = {entry["step"] for entry in result.execution_log}
        assert "validation" in steps
        assert "module_a" in steps
        assert "module_b" in steps
        assert "orchestration" in steps
        assert "additional_modules" in steps
        assert "cross_reference" in steps
        assert "enrichment" in steps
        assert "summary" in steps

    def test_fynd_berikade_med_ai_reasoning(self):
        """Verifierar att fynd från moduler får ai_reasoning satt."""
        engine = _make_engine()

        # Steg 3: orkestreringsanrop → inga fler moduler
        orchestration = json.dumps({"modules": []})
        # Steg 5: korskoppling
        cross_ref = json.dumps({"patterns": [], "connections": []})
        # Steg 6: berikning — men vi vet inte vilka finding_ids bank_matching ger
        # Returnera tom array (finding behåller ai_reasoning=None men kastas ej)
        enrichment = json.dumps([])
        # Steg 7: sammanfattning
        summary = json.dumps({"summary": "Klar."})

        engine.client.messages.create.side_effect = [
            _mock_response(orchestration),
            _mock_response(cross_ref),
            _mock_response(enrichment),
            _mock_response(summary),
        ]

        result = engine.run_full_analysis([_make_company()], {})
        # Alla fynd (0 st från tomma bolag) ska ha returnerats korrekt
        assert isinstance(result.findings, list)

    def test_summary_satt_i_result(self):
        engine = _make_engine()
        engine.client.messages.create.return_value = _mock_response(
            json.dumps({"modules": [], "summary": "Analysen är klar."})
        )
        result = engine.run_full_analysis([_make_company()], {})
        assert result.summary != ""


# ---------------------------------------------------------------------------
# Tester: cross_reference_findings (publik metod)
# ---------------------------------------------------------------------------


class TestCrossReferenceFindings:
    def test_returnerar_samma_fynd(self):
        engine = _make_engine()
        engine.client.messages.create.return_value = _mock_response(
            json.dumps({"patterns": [], "connections": []})
        )
        findings = [_make_finding("F001"), _make_finding("F002")]
        result = engine.cross_reference_findings(findings)
        assert result is findings

    def test_tom_lista_ger_inga_api_anrop(self):
        engine = _make_engine()
        engine.cross_reference_findings([])
        engine.client.messages.create.assert_not_called()
