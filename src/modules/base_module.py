"""Abstrakt basklass för alla analysmoduler."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.core.models import BankTransaction, Company, Finding


class AnalysisModule(ABC):
    """Basklass för alla analysmoduler i det forensiska systemet.

    Varje konkret modul implementerar `analyze()` och de abstrakta
    egenskaperna `name` och `description`.  Instansen håller en intern
    räknare för Finding-ID:n som garanterar unikhet inom en körning.
    """

    def __init__(self) -> None:
        self._finding_counter: int = 0

    # ------------------------------------------------------------------
    # Abstrakta egenskaper
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Modulens korta identifierare, t.ex. 'bank_matching'."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Kort beskrivning av vad modulen analyserar."""

    # ------------------------------------------------------------------
    # Abstrakt analysmetod
    # ------------------------------------------------------------------

    @abstractmethod
    def analyze(
        self,
        companies: list[Company],
        bank_transactions: dict[str, list[BankTransaction]],
        config: dict,
    ) -> list[Finding]:
        """Kör analysen och returnera fynd.

        Args:
            companies: Lista av Company-objekt (ett per bolag i sfären).
            bank_transactions: Dict med org_nr → lista av BankTransaction.
            config: Konfigurationsdict (läses från YAML-konfiguration).

        Returns:
            Lista av Finding-objekt, sorterade på risknivå (CRITICAL → INFO).
        """

    # ------------------------------------------------------------------
    # Hjälpmetod för att skapa Finding
    # ------------------------------------------------------------------

    def _create_finding(
        self,
        category: str,
        risk_level: str,
        summary: str,
        companies: list[str],
        details: dict,
    ) -> Finding:
        """Skapar ett Finding med auto-genererat ID.

        Finding-ID-format: ``MOD_<MODUL>_<NNN>`` där MODUL är de tre
        första bokstäverna i ``self.name`` (versaler) och NNN är ett
        nollpaddat löpnummer per modulinstans.

        Args:
            category: Fyndkategori, t.ex. ``'unmatched_bank'``.
            risk_level: ``CRITICAL``, ``HIGH``, ``MEDIUM``, ``LOW``, eller ``INFO``.
            summary: Kort sammanfattning (en mening).
            companies: Lista av org_nr för berörda bolag.
            details: Fri dict med detaljinformation.

        Returns:
            Nytt Finding-objekt.
        """
        self._finding_counter += 1
        module_code = self.name.upper().replace("_", "")[:3]
        finding_id = f"MOD_{module_code}_{self._finding_counter:03d}"
        return Finding(
            finding_id=finding_id,
            module=self.name,
            companies=companies,
            risk_level=risk_level,
            category=category,
            summary=summary,
            details=details,
        )
