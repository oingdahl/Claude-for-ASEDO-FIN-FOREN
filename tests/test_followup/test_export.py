"""Tester för export av utredningsjournal (Markdown)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from src.core.models import Company, Finding, FiscalYear
from src.followup.data_accessor import DataAccessor
from src.followup.investigator import Investigator
from src.followup.session import InvestigationSession

ORG_A = "556000-0001"


def _make_session() -> InvestigationSession:
    return InvestigationSession(
        session_id="export01",
        started_at=datetime(2026, 3, 6, 14, 0),
        config_path="config/default_config.yaml",
        results_path="output/results.json",
        context_path="output/context.json",
        source_sie_dir="test_data/",
        source_csv_dir="test_data/",
    )


def _make_company() -> Company:
    c = Company(org_nr=ORG_A, name="ExportTestAB", role="holding")
    c.fiscal_years.append(FiscalYear(0, date(2025, 1, 1), date(2025, 12, 31)))
    return c


def _make_finding(fid: str, risk: str = "HIGH") -> Finding:
    return Finding(
        finding_id=fid, module="test_module", companies=[ORG_A],
        risk_level=risk, category="test", summary=f"Fynd {fid}",
    )


def _make_investigator(tmp_path) -> Investigator:
    session = _make_session()
    company = _make_company()
    findings = [
        _make_finding("F001", "HIGH"),
        _make_finding("F002", "MEDIUM"),
        _make_finding("F003", "LOW"),
    ]
    accessor = DataAccessor([company], {}, findings)
    return Investigator(
        session=session,
        data_accessor=accessor,
        companies=[company],
        bank_transactions={},
        findings=findings,
        config={},
        api_key="test-key",
        sphere_name="Exporttestsfären",
    )


class TestExportInvestigation:
    def test_exporterar_markdown_fil(self, tmp_path):
        inv = _make_investigator(tmp_path)
        export_path = str(tmp_path / "journal.md")
        inv.export_investigation(export_path)
        assert Path(export_path).exists()

    def test_innehaller_sfärnamn(self, tmp_path):
        inv = _make_investigator(tmp_path)
        export_path = str(tmp_path / "journal.md")
        inv.export_investigation(export_path)
        content = Path(export_path).read_text(encoding="utf-8")
        assert "Exporttestsfären" in content

    def test_konversationshistorik_inkluderas(self, tmp_path):
        inv = _make_investigator(tmp_path)
        inv.session.conversation_history = [
            {"role": "user", "content": "Vilka HIGH-fynd finns?"},
            {"role": "assistant", "content": "Det finns ett HIGH-fynd: F001."},
        ]
        export_path = str(tmp_path / "journal.md")
        inv.export_investigation(export_path)
        content = Path(export_path).read_text(encoding="utf-8")
        assert "Vilka HIGH-fynd finns?" in content
        assert "Det finns ett HIGH-fynd" in content

    def test_noteringar_inkluderas(self, tmp_path):
        inv = _make_investigator(tmp_path)
        inv.add_note("F001", "Begär fakturakopia", status="escalated")
        export_path = str(tmp_path / "journal.md")
        inv.export_investigation(export_path)
        content = Path(export_path).read_text(encoding="utf-8")
        assert "F001" in content
        assert "Begär fakturakopia" in content
        assert "escalated" in content

    def test_korrekt_markdown_format(self, tmp_path):
        inv = _make_investigator(tmp_path)
        inv.session.conversation_history = [
            {"role": "user", "content": "Fråga 1"},
            {"role": "assistant", "content": "Svar 1"},
            {"role": "user", "content": "Fråga 2"},
            {"role": "assistant", "content": "Svar 2"},
        ]
        export_path = str(tmp_path / "journal.md")
        inv.export_investigation(export_path)
        content = Path(export_path).read_text(encoding="utf-8")

        assert "# Utredningsjournal" in content
        assert "## Frågor och svar" in content
        assert "### Fråga 1" in content
        assert "### Fråga 2" in content
        assert "## Sammanfattning" in content

    def test_inga_moduler_omkordes_visas(self, tmp_path):
        inv = _make_investigator(tmp_path)
        export_path = str(tmp_path / "journal.md")
        inv.export_investigation(export_path)
        content = Path(export_path).read_text(encoding="utf-8")
        assert "Inga moduler kördes om" in content

    def test_overkort_journal_med_omkorning(self, tmp_path):
        inv = _make_investigator(tmp_path)
        inv.session.rerun_log = [{
            "module": "bank_matching",
            "params": {"company_filter": "ExportTestAB"},
            "findings_count": 3,
            "timestamp": "2026-03-06T14:05:00",
        }]
        export_path = str(tmp_path / "journal.md")
        inv.export_investigation(export_path)
        content = Path(export_path).read_text(encoding="utf-8")
        assert "bank_matching" in content
        assert "findings_count" not in content  # Ska vara läsbart, inte raw JSON-nyckel
