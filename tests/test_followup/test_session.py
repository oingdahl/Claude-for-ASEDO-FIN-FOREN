"""Tester för src/followup/session.py."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.followup.session import InvestigationSession, SessionManager


def _make_session(**kwargs) -> InvestigationSession:
    from datetime import datetime
    defaults = dict(
        session_id="abc12345",
        started_at=datetime(2026, 3, 6, 12, 0, 0),
        config_path="config/default_config.yaml",
        results_path="output/results.json",
        context_path="output/context.json",
        source_sie_dir="test_data/",
        source_csv_dir="test_data/",
    )
    defaults.update(kwargs)
    return InvestigationSession(**defaults)


class TestInvestigationSession:
    def test_to_dict_roundtrip(self):
        session = _make_session()
        session.conversation_history = [
            {"role": "user", "content": "Fråga 1"},
            {"role": "assistant", "content": "Svar 1"},
        ]
        session.investigation_notes = [{"finding_id": "MOD_A_001", "note": "Misstänkt"}]
        session.rerun_log = [{"module": "bank_matching", "params": {}, "timestamp": "2026-03-06T12:00:00"}]

        d = session.to_dict()
        restored = InvestigationSession.from_dict(d)

        assert restored.session_id == session.session_id
        assert restored.config_path == session.config_path
        assert restored.conversation_history == session.conversation_history
        assert restored.investigation_notes == session.investigation_notes
        assert restored.rerun_log == session.rerun_log

    def test_from_dict_with_missing_optional_fields(self):
        from datetime import datetime
        data = {
            "session_id": "xyz",
            "started_at": "2026-01-01T10:00:00",
            "config_path": "c.yaml",
            "results_path": "r.json",
            "context_path": "ctx.json",
            "source_sie_dir": "sie/",
            "source_csv_dir": "csv/",
        }
        session = InvestigationSession.from_dict(data)
        assert session.conversation_history == []
        assert session.investigation_notes == []
        assert session.rerun_log == []


class TestSessionManager:
    def test_create_session(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path / "sessions"))
        session = mgr.create_session("c.yaml", "r.json", "ctx.json", "sie/", "csv/")

        assert len(session.session_id) == 8
        assert session.config_path == "c.yaml"
        assert session.conversation_history == []

    def test_save_and_load_session(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path / "sessions"))
        session = mgr.create_session("c.yaml", "r.json", "ctx.json", "sie/", "csv/")
        session.conversation_history = [{"role": "user", "content": "Test"}]

        saved_path = mgr.save_session(session)
        assert Path(saved_path).exists()

        loaded = mgr.load_session(saved_path)
        assert loaded.session_id == session.session_id
        assert loaded.conversation_history == session.conversation_history

    def test_save_creates_sessions_dir(self, tmp_path):
        sessions_dir = str(tmp_path / "deep" / "sessions")
        mgr = SessionManager(sessions_dir=sessions_dir)
        session = mgr.create_session("c.yaml", "r.json", "ctx.json", "sie/", "csv/")
        mgr.save_session(session)
        assert Path(sessions_dir).exists()

    def test_list_sessions_empty(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path / "sessions"))
        assert mgr.list_sessions() == []

    def test_list_sessions_returns_summaries(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path / "sessions"))
        session = mgr.create_session("c.yaml", "r.json", "ctx.json", "sie/", "csv/")
        session.conversation_history = [
            {"role": "user", "content": "Fråga"},
            {"role": "assistant", "content": "Svar"},
        ]
        mgr.save_session(session)

        summaries = mgr.list_sessions()
        assert len(summaries) == 1
        assert summaries[0]["session_id"] == session.session_id
        assert summaries[0]["questions"] == 1

    def test_konversationshistorik_sparas_korrekt(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path / "sessions"))
        session = mgr.create_session("c.yaml", "r.json", "ctx.json", "sie/", "csv/")
        session.conversation_history.append({"role": "user", "content": "Vilka fynd finns?"})
        session.conversation_history.append({"role": "assistant", "content": "Det finns 5 fynd."})

        path = mgr.save_session(session)
        loaded = mgr.load_session(path)

        assert len(loaded.conversation_history) == 2
        assert loaded.conversation_history[0]["content"] == "Vilka fynd finns?"

    def test_noteringar_kopplas_till_fynd(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path / "sessions"))
        session = mgr.create_session("c.yaml", "r.json", "ctx.json", "sie/", "csv/")
        session.investigation_notes.append({
            "finding_id": "MOD_A_001",
            "note": "Begär fakturakopia",
            "status": "escalated",
        })

        path = mgr.save_session(session)
        loaded = mgr.load_session(path)

        assert len(loaded.investigation_notes) == 1
        assert loaded.investigation_notes[0]["finding_id"] == "MOD_A_001"
        assert loaded.investigation_notes[0]["status"] == "escalated"
