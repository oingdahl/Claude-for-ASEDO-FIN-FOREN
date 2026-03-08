"""Modul A: Avstämning bank-CSV mot bokföring (SIE4).

Matchningsalgoritm (i prioritetsordning):
  1. Stark match  — belopp ±tolerans OCH datum ±3 dagar
  2. Svag match   — belopp ±tolerans, datum utanför ±3 dagar
  3. Omatchad     — ingen match hittades

Fynd som genereras:
  - unmatched_bank   HIGH   Bankpost utan motpart i bokföringen
  - unmatched_book   HIGH   Bokföringspost på bankkonto utan motpart i banken
  - date_discrepancy MEDIUM Match på belopp men >5 dagars datumskillnad
  - amount_discrepancy MEDIUM Match med beloppsavvikelse 1-50 kr
  - double_match     HIGH   Bokföringspost matchad mot flera bankposter
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from src.core.models import BankTransaction, Company, Finding, Voucher, Transaction
from src.modules.base_module import AnalysisModule

logger = logging.getLogger(__name__)

# Konton som räknas som bankkonton (SEB: 1910-1940)
_DEFAULT_BANK_ACCOUNT_START = 1910
_DEFAULT_BANK_ACCOUNT_END = 1940
_DEFAULT_TOLERANCE_KR = 50  # kr
_STRONG_DATE_WINDOW = 3     # dagar
_DATE_DISCREPANCY_THRESHOLD = 5  # dagar


# ---------------------------------------------------------------------------
# Interna datastrukturer
# ---------------------------------------------------------------------------

@dataclass
class _BookEntry:
    """En bokföringspost på ett bankkonto."""
    org_nr: str
    voucher: Voucher
    transaction: Transaction
    matched_by_bank_idx: list[int] = field(default_factory=list)  # index i _BankEntry-listan


@dataclass
class _BankEntry:
    """En banktransaktion från CSV."""
    tx: BankTransaction
    matched_book_idxs: list[int] = field(default_factory=list)  # index i _BookEntry-listan
    match_type: str = ""  # "strong" | "weak" | ""


# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

def _is_bank_account(account_nr: str, start: int, end: int) -> bool:
    try:
        nr = int(account_nr)
        return start <= nr <= end
    except ValueError:
        return False


def _date_diff_days(d1: date, d2: date) -> int:
    return abs((d1 - d2).days)


def _tx_key(org_nr: str, voucher: Voucher, trans: Transaction) -> dict:
    """Returnerar en serialiserbar dict med transaktionsinformation."""
    return {
        "org_nr": org_nr,
        "voucher_series": voucher.series,
        "voucher_number": voucher.number,
        "voucher_date": str(voucher.date),
        "voucher_text": voucher.text,
        "account": trans.account,
        "amount_oren": trans.amount,
        "amount_kr": trans.amount / 100,
        "trans_text": trans.text or "",
    }


def _bank_key(tx: BankTransaction) -> dict:
    return {
        "booking_date": str(tx.booking_date),
        "text": tx.text,
        "transaction_type": tx.transaction_type,
        "amount_oren": tx.amount,
        "amount_kr": tx.amount / 100,
        "running_balance_oren": tx.running_balance,
        "source_row": tx.source_row,
        "source_file": tx.source_file,
    }


# ---------------------------------------------------------------------------
# Modulklass
# ---------------------------------------------------------------------------

class BankMatchingModule(AnalysisModule):
    """Modul A: Bank vs Huvudbok."""

    @property
    def name(self) -> str:
        return "bank_matching"

    @property
    def description(self) -> str:
        return (
            "Stämmer av banktransaktioner (CSV) mot bokföringsposter på "
            "bankkonton (1910–1940) i SIE4-data.  Flaggar omatchade poster, "
            "datumavvikelser och beloppsskillnader."
        )

    def analyze(
        self,
        companies: list[Company],
        bank_transactions: dict[str, list[BankTransaction]],
        config: dict,
    ) -> list[Finding]:
        """Kör bank-bokföringsavstämning för alla bolag i sfären.

        Args:
            companies: Bolag att analysera.
            bank_transactions: org_nr → lista av BankTransaction.
            config: Konfiguration.  Relevant nyckel:
                ``config['matching']['amount_tolerance_bank_kr']`` (default 50).

        Returns:
            Lista av Finding sorterade på risk (HIGH → MEDIUM).
        """
        tolerance_kr: int = (
            config.get("matching", {}).get("amount_tolerance_bank_kr", _DEFAULT_TOLERANCE_KR)
        )
        tolerance_oren: int = tolerance_kr * 100

        bank_acc_start: int = (
            config.get("matching", {}).get("bank_account_start", _DEFAULT_BANK_ACCOUNT_START)
        )
        bank_acc_end: int = (
            config.get("matching", {}).get("bank_account_end", _DEFAULT_BANK_ACCOUNT_END)
        )

        findings: list[Finding] = []

        for company in companies:
            org_nr = company.org_nr
            bank_txs = bank_transactions.get(org_nr, [])

            if not bank_txs:
                logger.debug("Inga banktransaktioner för %s (%s)", company.name, org_nr)
                findings.extend(
                    self._handle_no_bank_data(company, org_nr, bank_acc_start, bank_acc_end)
                )
                continue

            findings.extend(
                self._match_company(
                    company, org_nr, bank_txs,
                    tolerance_oren, bank_acc_start, bank_acc_end,
                )
            )

        # Sortera: HIGH före MEDIUM
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
        findings.sort(key=lambda f: order.get(f.risk_level, 99))
        return findings

    # ------------------------------------------------------------------
    # Intern hjälplogik: bolag utan bank-CSV
    # ------------------------------------------------------------------

    def _handle_no_bank_data(
        self,
        company: Company,
        org_nr: str,
        acc_start: int,
        acc_end: int,
    ) -> list[Finding]:
        """Hanterar bolag utan bank-CSV: flaggar bokföringsposter på bankkonton."""
        book_entries = [
            (v, t)
            for v in company.vouchers
            for t in v.transactions
            if _is_bank_account(t.account, acc_start, acc_end)
        ]
        if not book_entries:
            return [self._create_finding(
                category="no_data",
                risk_level="INFO",
                summary=(
                    f"{company.name} ({org_nr}): Ingen bank-CSV tillgänglig och "
                    f"inga bokföringsposter på bankkonton"
                ),
                companies=[org_nr],
                details={"org_nr": org_nr, "reason": "no_bank_csv_and_no_book_entries"},
            )]
        findings = []
        for v, t in book_entries:
            findings.append(self._create_finding(
                category="unmatched_book",
                risk_level="HIGH",
                summary=(
                    f"Bokföringspost på bankkonto saknar bankmotsvarighet (ingen bank-CSV): "
                    f"{v.date} {t.amount / 100:.2f} kr konto {t.account}"
                ),
                companies=[org_nr],
                details={"book": _tx_key(org_nr, v, t), "reason": "no_bank_csv"},
            ))
        return findings

    # ------------------------------------------------------------------
    # Intern matchningslogik per bolag
    # ------------------------------------------------------------------

    def _match_company(
        self,
        company: Company,
        org_nr: str,
        bank_txs: list[BankTransaction],
        tolerance_oren: int,
        acc_start: int,
        acc_end: int,
    ) -> list[Finding]:
        findings: list[Finding] = []

        # Bygg lista av bokföringsposter på bankkonton
        book_entries: list[_BookEntry] = []
        for v in company.vouchers:
            for t in v.transactions:
                if _is_bank_account(t.account, acc_start, acc_end):
                    book_entries.append(_BookEntry(org_nr=org_nr, voucher=v, transaction=t))

        if not book_entries:
            logger.debug(
                "Inga bokföringsposter på bankkonton %d-%d för %s",
                acc_start, acc_end, org_nr,
            )

        bank_entries: list[_BankEntry] = [_BankEntry(tx=b) for b in bank_txs]

        logger.info(
            "%s: %d bokföringsposter, %d bankposter att stämma av",
            org_nr, len(book_entries), len(bank_entries),
        )

        # Steg 1 & 2: Matcha bank → bok (greedy, starka matcher prioriteras)
        for bi, be in enumerate(bank_entries):
            if be.matched_book_idxs:
                continue

            # Leta stark match
            for bki, bke in enumerate(book_entries):
                if bke.matched_by_bank_idx:
                    continue
                amt_diff = abs(be.tx.amount - bke.transaction.amount)
                date_diff = _date_diff_days(be.tx.booking_date, bke.voucher.date)
                if amt_diff <= tolerance_oren and date_diff <= _STRONG_DATE_WINDOW:
                    be.matched_book_idxs.append(bki)
                    bke.matched_by_bank_idx.append(bi)
                    be.match_type = "strong"
                    break

        # Steg 2: Svag match (enbart belopp) för de som fortfarande är omatchade
        for bi, be in enumerate(bank_entries):
            if be.matched_book_idxs:
                continue
            for bki, bke in enumerate(book_entries):
                if bke.matched_by_bank_idx:
                    continue
                amt_diff = abs(be.tx.amount - bke.transaction.amount)
                if amt_diff <= tolerance_oren:
                    be.matched_book_idxs.append(bki)
                    bke.matched_by_bank_idx.append(bi)
                    be.match_type = "weak"
                    break

        # Generera fynd från matchade par (datum- och beloppsavvikelser)
        for bi, be in enumerate(bank_entries):
            for bki in be.matched_book_idxs:
                bke = book_entries[bki]
                amt_diff = abs(be.tx.amount - bke.transaction.amount)
                date_diff = _date_diff_days(be.tx.booking_date, bke.voucher.date)

                if date_diff > _DATE_DISCREPANCY_THRESHOLD:
                    findings.append(self._create_finding(
                        category="date_discrepancy",
                        risk_level="MEDIUM",
                        summary=(
                            f"Beloppsmatch men {date_diff} dagars datumavvikelse "
                            f"({be.tx.booking_date} vs {bke.voucher.date})"
                        ),
                        companies=[org_nr],
                        details={
                            "bank": _bank_key(be.tx),
                            "book": _tx_key(org_nr, bke.voucher, bke.transaction),
                            "date_diff_days": date_diff,
                            "amount_diff_oren": amt_diff,
                        },
                    ))

                if 0 < amt_diff <= tolerance_oren:
                    findings.append(self._create_finding(
                        category="amount_discrepancy",
                        risk_level="MEDIUM",
                        summary=(
                            f"Nästan-match: beloppsavvikelse {amt_diff / 100:.2f} kr "
                            f"(bank {be.tx.amount / 100:.2f} kr, "
                            f"bok {bke.transaction.amount / 100:.2f} kr)"
                        ),
                        companies=[org_nr],
                        details={
                            "bank": _bank_key(be.tx),
                            "book": _tx_key(org_nr, bke.voucher, bke.transaction),
                            "amount_diff_oren": amt_diff,
                            "amount_diff_kr": amt_diff / 100,
                        },
                    ))

        # Dubbelträff: bokföringspost matchad mot flera bankposter
        for bki, bke in enumerate(book_entries):
            if len(bke.matched_by_bank_idx) > 1:
                bank_details = [
                    _bank_key(bank_entries[bi].tx) for bi in bke.matched_by_bank_idx
                ]
                findings.append(self._create_finding(
                    category="double_match",
                    risk_level="HIGH",
                    summary=(
                        f"Bokföringspost matchad mot {len(bke.matched_by_bank_idx)} bankposter"
                    ),
                    companies=[org_nr],
                    details={
                        "book": _tx_key(org_nr, bke.voucher, bke.transaction),
                        "matched_bank_entries": bank_details,
                    },
                ))

        # Omatchade bankposter
        for be in bank_entries:
            if not be.matched_book_idxs:
                findings.append(self._create_finding(
                    category="unmatched_bank",
                    risk_level="HIGH",
                    summary=(
                        f"Banktransaktion saknar motpart i bokföringen: "
                        f"{be.tx.booking_date} {be.tx.amount / 100:.2f} kr '{be.tx.text}'"
                    ),
                    companies=[org_nr],
                    details={"bank": _bank_key(be.tx)},
                ))

        # Omatchade bokföringsposter
        for bke in book_entries:
            if not bke.matched_by_bank_idx:
                findings.append(self._create_finding(
                    category="unmatched_book",
                    risk_level="HIGH",
                    summary=(
                        f"Bokföringspost på bankkonto saknar motpart i banken: "
                        f"{bke.voucher.date} {bke.transaction.amount / 100:.2f} kr "
                        f"konto {bke.transaction.account}"
                    ),
                    companies=[org_nr],
                    details={"book": _tx_key(org_nr, bke.voucher, bke.transaction)},
                ))

        logger.info(
            "%s: %d fynd genererade",
            org_nr, len(findings),
        )
        return findings
