"""Datakvalitetsvalidering av parsad SIE4- och CSV-data.

Producerar en ValidationReport per datafil med räknekontroller, saldo-
validering och varningar. Körs EFTER parsning som ett oberoende steg.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import chardet

from src.core.models import BankTransaction, Company

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rapport-dataclass
# ---------------------------------------------------------------------------

@dataclass
class ValidationReport:
    """Sammanfattning av validering för en enskild källfil."""

    file_path: str
    file_type: str          # "SIE4" eller "CSV"
    checksum: str           # SHA-256 av råfil
    timestamp: datetime
    total_records: int      # Förväntade poster (räknade i källfil)
    parsed_records: int     # Faktiskt parsade poster
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    is_valid: bool = True   # True om inga errors (warnings är OK)
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Interna hjälpare
# ---------------------------------------------------------------------------

def _sha256(filepath: str) -> str:
    return hashlib.sha256(Path(filepath).read_bytes()).hexdigest()


def _count_pattern_in_sie(filepath: str, pattern: str) -> int:
    """Räknar matchande rader i SIE-filen (utan att full-parsa den)."""
    raw = Path(filepath).read_bytes()
    # Detektera kodning
    if raw.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8-sig"
    else:
        res = chardet.detect(raw[:4096])
        enc = res.get("encoding") or "utf-8"
    text = raw.decode(enc, errors="replace")
    rx = re.compile(pattern, re.MULTILINE)
    return len(rx.findall(text))


def _count_data_rows_csv(filepath: str) -> int:
    """Räknar icke-tomma datarader efter headern i CSV-filen."""
    raw = Path(filepath).read_bytes()
    enc = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    text = raw.decode(enc, errors="replace")
    lines = text.splitlines()
    return sum(1 for ln in lines[1:] if ln.strip())


# ---------------------------------------------------------------------------
# Publik API
# ---------------------------------------------------------------------------

def validate_sie4(company: Company, source_path: str) -> ValidationReport:
    """Validerar parsad SIE4-data mot källfil.

    Kontroller:
    - Verifikationsantal (parsad vs källfil)
    - Transaktionsantal (parsad vs källfil)
    - Kontoplanantal (parsad vs källfil)
    - Balans per verifikation (debet = kredit ±1 öre)
    - Inga tomma verifikationstexter (varning)
    - Inga duplicerade verifikationsnummer inom samma serie

    Args:
        company: Parsad Company-instans.
        source_path: Sökväg till original-SIE4-filen.

    Returns:
        ValidationReport med is_valid=True om inga errors hittades.
    """
    ts = datetime.now()
    checksum = _sha256(source_path)
    warnings: list[str] = []
    errors: list[str] = []
    details: dict = {}

    # Räkna i källfil
    expected_ver = _count_pattern_in_sie(source_path, r"^\s*#VER\b")
    expected_trans = _count_pattern_in_sie(source_path, r"^\s*#TRANS\b")
    expected_konto = _count_pattern_in_sie(source_path, r"^\s*#KONTO\b")

    actual_ver = len(company.vouchers)
    actual_trans = sum(len(v.transactions) for v in company.vouchers)
    actual_konto = len(company.chart_of_accounts)

    details["ver_expected"] = expected_ver
    details["ver_parsed"] = actual_ver
    details["trans_expected"] = expected_trans
    details["trans_parsed"] = actual_trans
    details["konto_expected"] = expected_konto
    details["konto_parsed"] = actual_konto

    if actual_ver != expected_ver:
        errors.append(
            f"Verifikationsantal: förväntat {expected_ver}, parsade {actual_ver}"
        )
    if actual_trans != expected_trans:
        errors.append(
            f"Transaktionsantal: förväntat {expected_trans}, parsade {actual_trans}"
        )
    if actual_konto != expected_konto:
        errors.append(
            f"Kontoplanantal: förväntat {expected_konto}, parsade {actual_konto}"
        )

    # Verifikationsbalans (debet = kredit ±1 öre)
    unbalanced = []
    for v in company.vouchers:
        total = sum(t.amount for t in v.transactions)
        if abs(total) > 1:
            unbalanced.append(f"{v.series}{v.number} ({total} öre)")
    if unbalanced:
        errors.append(
            f"Obalanserade verifikationer ({len(unbalanced)}): "
            + ", ".join(unbalanced[:5])
            + ("…" if len(unbalanced) > 5 else "")
        )
    details["unbalanced_vouchers"] = len(unbalanced)

    # Tomma verifikationstexter (varning)
    empty_text = [
        f"{v.series}{v.number}" for v in company.vouchers if not v.text.strip()
    ]
    if empty_text:
        warnings.append(
            f"Tomma verifikationstexter ({len(empty_text)}): "
            + ", ".join(empty_text[:5])
            + ("…" if len(empty_text) > 5 else "")
        )
    details["empty_text_vouchers"] = len(empty_text)

    # Duplicerade verifikationsnummer inom samma serie
    seen: dict[tuple[str, int], int] = {}
    dupes = []
    for v in company.vouchers:
        key = (v.series, v.number)
        if key in seen:
            dupes.append(f"{v.series}{v.number}")
        else:
            seen[key] = 1
    if dupes:
        errors.append(
            f"Duplicerade verifikationsnummer ({len(dupes)}): "
            + ", ".join(dupes[:5])
            + ("…" if len(dupes) > 5 else "")
        )
    details["duplicate_voucher_numbers"] = len(dupes)

    # IB + rörelser = UB per konto (om data finns)
    ib_ub_errors = []
    if company.opening_balances and company.closing_balances:
        for acc_nr, ib in company.opening_balances.items():
            ub = company.closing_balances.get(acc_nr)
            if ub is None:
                continue
            movement = sum(
                t.amount
                for v in company.vouchers
                for t in v.transactions
                if t.account == acc_nr
            )
            # IB och UB lagras som Decimal (kr), rörelse är i öre
            calc_ub = ib + Decimal(movement) / 100
            diff = abs(calc_ub - ub)
            if diff > Decimal("0.02"):
                ib_ub_errors.append(
                    f"Konto {acc_nr}: IB {ib} + rörelser {movement / 100:.2f} "
                    f"= {calc_ub:.2f} ≠ UB {ub} (diff {diff:.2f})"
                )
    if ib_ub_errors:
        warnings.append(
            f"IB+rörelser≠UB för {len(ib_ub_errors)} konton: "
            + "; ".join(ib_ub_errors[:3])
            + ("…" if len(ib_ub_errors) > 3 else "")
        )
    details["ib_ub_mismatches"] = len(ib_ub_errors)

    is_valid = len(errors) == 0

    return ValidationReport(
        file_path=source_path,
        file_type="SIE4",
        checksum=checksum,
        timestamp=ts,
        total_records=expected_ver,
        parsed_records=actual_ver,
        warnings=warnings,
        errors=errors,
        is_valid=is_valid,
        details=details,
    )


def validate_bank_csv(
    transactions: list[BankTransaction],
    source_path: str,
) -> ValidationReport:
    """Validerar parsade banktransaktioner mot källfil.

    Kontroller:
    - Radantal (parsad vs källfil)
    - Löpande saldo (rad för rad, ±1 öre tolerans)
    - Inga tomma datumfält
    - Belopp 0 (varning)
    - Datumordning (ska vara fallande för SEB-format)

    Args:
        transactions: Lista av BankTransaction från import_bank_csv.
        source_path: Sökväg till original-CSV-filen.

    Returns:
        ValidationReport med is_valid=True om inga errors hittades.
    """
    ts = datetime.now()
    checksum = _sha256(source_path)
    warnings: list[str] = []
    errors: list[str] = []
    details: dict = {}

    expected_rows = _count_data_rows_csv(source_path)
    parsed_rows = len(transactions)

    details["rows_expected"] = expected_rows
    details["rows_parsed"] = parsed_rows

    if parsed_rows != expected_rows:
        errors.append(
            f"Radantal: förväntat {expected_rows}, parsade {parsed_rows}"
        )

    # Löpande saldo-kontroll
    saldo_errors = 0
    for i in range(len(transactions) - 1):
        curr = transactions[i]
        nxt = transactions[i + 1]
        expected_nxt_bal = curr.running_balance - curr.amount
        diff = abs(nxt.running_balance - expected_nxt_bal)
        if diff > 1:
            saldo_errors += 1
            logger.debug(
                "Saldoavvikelse rad %d→%d: diff %d öre",
                curr.source_row,
                nxt.source_row,
                diff,
            )
    if saldo_errors:
        errors.append(
            f"Löpande saldo stämmer inte för {saldo_errors} radövergångar"
        )
    details["balance_errors"] = saldo_errors

    # Inga tomma datum
    null_dates = [t.source_row for t in transactions if not t.booking_date]
    if null_dates:
        errors.append(f"Tomma bokföringsdatum på {len(null_dates)} rader: {null_dates[:5]}")
    details["null_date_rows"] = len(null_dates)

    # Belopp 0-varning
    zero_amounts = [t.source_row for t in transactions if t.amount == 0]
    if zero_amounts:
        warnings.append(
            f"Belopp exakt 0 på {len(zero_amounts)} rader: {zero_amounts[:5]}"
        )
    details["zero_amount_rows"] = len(zero_amounts)

    # Datumordning (ska vara fallande)
    order_violations = 0
    for i in range(len(transactions) - 1):
        if transactions[i].booking_date < transactions[i + 1].booking_date:
            order_violations += 1
    if order_violations:
        warnings.append(
            f"Datumordningen är inte strikt fallande: {order_violations} avvikelser"
        )
    details["date_order_violations"] = order_violations

    is_valid = len(errors) == 0

    return ValidationReport(
        file_path=source_path,
        file_type="CSV",
        checksum=checksum,
        timestamp=ts,
        total_records=expected_rows,
        parsed_records=parsed_rows,
        warnings=warnings,
        errors=errors,
        is_valid=is_valid,
        details=details,
    )


def print_validation_summary(reports: list[ValidationReport]) -> None:
    """Skriver ut en tydlig sammanfattning av alla valideringsrapporter."""
    SEP = "=" * 72
    print(SEP)
    print("  DATAKVALITETSRAPPORT")
    print(SEP)

    for r in reports:
        status = "OK" if r.is_valid else "FEL"
        print(f"\n[{status}] {r.file_type}: {Path(r.file_path).name}")
        print(f"       Sökväg    : {r.file_path}")
        print(f"       SHA-256   : {r.checksum[:20]}…")
        print(f"       Tidpunkt  : {r.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        print(
            f"       Poster    : {r.parsed_records} parsade / "
            f"{r.total_records} förväntade"
        )

        if r.details:
            print("       Detaljer  :", end="")
            items = list(r.details.items())
            for j, (k, v) in enumerate(items):
                prefix = "                  " if j > 0 else " "
                print(f"{prefix}{k}: {v}")

        if r.warnings:
            print(f"       Varningar ({len(r.warnings)}):")
            for w in r.warnings:
                print(f"         ⚠  {w}")

        if r.errors:
            print(f"       FEL ({len(r.errors)}):")
            for e in r.errors:
                print(f"         ✗  {e}")

    print(f"\n{SEP}")
    ok = sum(1 for r in reports if r.is_valid)
    print(f"  TOTALT: {ok}/{len(reports)} filer godkända")
    print(SEP)
