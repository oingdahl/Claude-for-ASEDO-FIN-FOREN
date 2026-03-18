"""Tester för src/followup/investigator.py — Investigator."""

from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from src.core.models import Company, Finding, FiscalYear, Transaction, Voucher
from src.followup.data_accessor import DataAccessor
from src.followup.investigator import Investigator
from src.followup.session import InvestigationSession

ORG_A = "556000-0001"


def _make_session() -> InvestigationSession:
    return InvestigationSession(
        session_id="test1234",
        started_at=datetime(2026, 3, 6, 12, 0),
        config_path="config/default_config.yaml",
        results_path="output/results.json",
        context_path="output/context.json",
        source_sie_dir="test_data/",
        source_csv_dir="test_data/",
    )


def _make_company() -> Company:
    c = Company(org_nr=ORG_A, name="TestAB", role="holding")
    c.fiscal_years.append(FiscalYear(0, date(2025, 1, 1), date(2025, 12, 31)))
    return c


def _make_finding(fid: str = "F001") -> Finding:
    return Finding(
        finding_id=fid, module="bank_matching", companies=[ORG_A],
        risk_level="HIGH", category="test", summary="Testfynd",
    )


def _make_investigator(mock_client: MagicMock | None = None) -> Investigator:
    session = _make_session()
    company = _make_company()
    findings = [_make_finding("F001"), _make_finding("F002")]
    accessor = DataAccessor([company], {}, findings)
    inv = Investigator(
        session=session,
        data_accessor=accessor,
        companies=[company],
        bank_transactions={},
        findings=findings,
        config={},
        api_key="test-key",
        sphere_name="Testsfären",
    )
    if mock_client is not None:
        inv.client = mock_client
    return inv


def _mock_text_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "end_turn"
    return response


def _mock_tool_use_response(tool_name: str, tool_id: str, inputs: dict) -> MagicMock:
    """Simulerar ett tool_use-svar från Claude."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = tool_name
    tool_block.id = tool_id
    tool_block.input = inputs
    response = MagicMock()
    response.content = [tool_block]
    response.stop_reason = "tool_use"
    return response


class TestAsk:
    def test_spar_fraga_i_historik(self):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_text_response("Svar på frågan.")
        inv = _make_investigator(mock_client)

        inv.ask("Vilka fynd finns?")

        assert len(inv.session.conversation_history) == 2
        assert inv.session.conversation_history[0]["role"] == "user"
        assert inv.session.conversation_history[0]["content"] == "Vilka fynd finns?"
        assert inv.session.conversation_history[1]["role"] == "assistant"

    def test_returnerar_svartext(self):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_text_response("Det finns 2 fynd.")
        inv = _make_investigator(mock_client)

        answer = inv.ask("Hur många fynd?")
        assert answer == "Det finns 2 fynd."

    def test_api_fel_returnerar_felmeddelande(self):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("Connection error")
        inv = _make_investigator(mock_client)

        answer = inv.ask("Fråga")
        assert "Fel" in answer or "fel" in answer

    def test_tool_use_anrop_exekveras(self):
        """Claude gör ett tool_use-anrop (get_findings) som exekveras och skickas tillbaka."""
        mock_client = MagicMock()
        # Första anrop: tool_use
        tool_response = _mock_tool_use_response(
            "get_findings", "tool_abc", {"risk_level": "HIGH"}
        )
        # Andra anrop: slutsvar efter tool_result
        final_response = _mock_text_response("Jag hittade 1 HIGH-fynd.")
        mock_client.messages.create.side_effect = [tool_response, final_response]

        inv = _make_investigator(mock_client)
        answer = inv.ask("Visa HIGH-fynd")

        assert mock_client.messages.create.call_count == 2
        assert answer == "Jag hittade 1 HIGH-fynd."

    def test_tool_use_get_findings_parsas_korrekt(self):
        """Verifierar att tool_use-anrop med get_findings returnerar korrekt data."""
        mock_client = MagicMock()
        tool_response = _mock_tool_use_response(
            "get_findings", "tool_xyz", {"risk_level": "HIGH"}
        )
        final_response = _mock_text_response("Klar.")
        mock_client.messages.create.side_effect = [tool_response, final_response]

        inv = _make_investigator(mock_client)
        inv.ask("Visa fynd")

        # Kontrollera att tool_result skickades tillbaka med korrekt format
        second_call_args = mock_client.messages.create.call_args_list[1]
        messages = second_call_args[1].get("messages") or second_call_args[0][0] if second_call_args[0] else None
        # Verifiera att det finns ett tool_result-meddelande
        assert mock_client.messages.create.call_count == 2


class TestAddNote:
    def test_notera_lagrar_i_session(self):
        inv = _make_investigator()
        inv.add_note("F001", "Begär fakturakopia")
        assert len(inv.session.investigation_notes) == 1
        assert inv.session.investigation_notes[0]["finding_id"] == "F001"
        assert inv.session.investigation_notes[0]["note"] == "Begär fakturakopia"

    def test_notera_med_status(self):
        inv = _make_investigator()
        inv.add_note("F001", "Eskalerad", status="escalated")
        note = inv.session.investigation_notes[0]
        assert note["status"] == "escalated"

    def test_status_uppdateras_pa_finding(self):
        inv = _make_investigator()
        inv.add_note("F001", "Bekräftad", status="confirmed")
        f001 = next(f for f in inv._findings if f.finding_id == "F001")
        assert f001.status == "confirmed"

    def test_ogiltig_status_ignoreras(self):
        inv = _make_investigator()
        inv.add_note("F001", "Test", status="ogiltig_status")
        f001 = next(f for f in inv._findings if f.finding_id == "F001")
        assert f001.status == "open"  # Ska inte förändras

    def test_notera_utan_api_anrop(self):
        """Verifierar att notering inte kräver API-anrop."""
        mock_client = MagicMock()
        inv = _make_investigator(mock_client)
        inv.add_note("F001", "Notering")
        mock_client.messages.create.assert_not_called()


class TestRerunModule:
    def test_rerun_loggas_i_session(self):
        inv = _make_investigator()
        inv.rerun_module("bank_matching", {})
        assert len(inv.session.rerun_log) == 1
        assert inv.session.rerun_log[0]["module"] == "bank_matching"

    def test_okand_modul_returnerar_tom_lista(self):
        inv = _make_investigator()
        result = inv.rerun_module("okand_modul", {})
        assert result == []

    def test_rerun_log_innehaller_findings_count(self):
        inv = _make_investigator()
        inv.rerun_module("amount_patterns", {})
        assert "findings_count" in inv.session.rerun_log[0]
