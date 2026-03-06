"""Sessionshantering för interaktivt uppföljningsläge."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class InvestigationSession:
    """En pågående granskningssession med full historik."""

    session_id: str
    started_at: datetime
    config_path: str
    results_path: str
    context_path: str
    source_sie_dir: str
    source_csv_dir: str
    conversation_history: list[dict] = field(default_factory=list)
    investigation_notes: list[dict] = field(default_factory=list)
    rerun_log: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "config_path": self.config_path,
            "results_path": self.results_path,
            "context_path": self.context_path,
            "source_sie_dir": self.source_sie_dir,
            "source_csv_dir": self.source_csv_dir,
            "conversation_history": self.conversation_history,
            "investigation_notes": self.investigation_notes,
            "rerun_log": self.rerun_log,
        }

    @classmethod
    def from_dict(cls, data: dict) -> InvestigationSession:
        return cls(
            session_id=data["session_id"],
            started_at=datetime.fromisoformat(data["started_at"]),
            config_path=data["config_path"],
            results_path=data["results_path"],
            context_path=data["context_path"],
            source_sie_dir=data["source_sie_dir"],
            source_csv_dir=data["source_csv_dir"],
            conversation_history=data.get("conversation_history", []),
            investigation_notes=data.get("investigation_notes", []),
            rerun_log=data.get("rerun_log", []),
        )


class SessionManager:
    """Hanterar skapande, sparning och laddning av granskningssessioner.

    Sessioner sparas automatiskt som JSON-filer i sessions-mappen.
    """

    def __init__(self, sessions_dir: str = "output/sessions") -> None:
        self.sessions_dir = sessions_dir

    def create_session(
        self,
        config_path: str,
        results_path: str,
        context_path: str,
        sie_dir: str,
        csv_dir: str,
    ) -> InvestigationSession:
        """Skapar en ny session kopplad till en analysomgång."""
        return InvestigationSession(
            session_id=uuid.uuid4().hex[:8],
            started_at=datetime.now(),
            config_path=config_path,
            results_path=results_path,
            context_path=context_path,
            source_sie_dir=sie_dir,
            source_csv_dir=csv_dir,
        )

    def save_session(self, session: InvestigationSession) -> str:
        """Sparar sessionen till disk. Returnerar filsökvägen."""
        Path(self.sessions_dir).mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"session_{session.session_id}_{timestamp}.json"
        filepath = Path(self.sessions_dir) / filename
        filepath.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return str(filepath)

    def load_session(self, session_path: str) -> InvestigationSession:
        """Laddar en tidigare session från disk."""
        data = json.loads(Path(session_path).read_text(encoding="utf-8"))
        return InvestigationSession.from_dict(data)

    def list_sessions(self, sessions_dir: str | None = None) -> list[dict]:
        """Listar alla sparade sessioner med sammanfattning."""
        directory = Path(sessions_dir or self.sessions_dir)
        if not directory.exists():
            return []
        summaries = []
        for path in sorted(directory.glob("session_*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                summaries.append({
                    "path": str(path),
                    "session_id": data.get("session_id", "?"),
                    "started_at": data.get("started_at", ""),
                    "questions": len(data.get("conversation_history", [])) // 2,
                    "notes": len(data.get("investigation_notes", [])),
                })
            except (json.JSONDecodeError, KeyError):
                pass
        return summaries
