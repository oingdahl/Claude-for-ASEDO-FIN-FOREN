"""Importmodul för bankutdrag (CSV).

Nolltolerans för dataförlust: parsningen avslutas med CSVImportError
om antal rader inte stämmer eller förväntade kolumner saknas.
Löpande saldo valideras rad för rad (med ±1 öre tolerans för bankens avrundning).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from pathlib import Path

import chardet

from src.core.models import BankTransaction

logger = logging.getLogger(__name__)

# Default SEB-format (kan åsidosättas via config)
_DEFAULT_SEB_CONFIG: dict = {
    "encoding": "utf-8-sig",
    "delimiter": ";",
    "columns": {
        "booking_date": "Bokförd",
        "value_date": "Valutadatum",
        "text": "Text",
        "transaction_type": "Typ",
        "deposits": "Insättningar",
        "withdrawals": "Uttag",
        "balance": "Bokfört saldo",
    },
    "date_format": "%Y-%m-%d",
    "decimal_separator": ",",
    "thousands_separator": " ",
}


class CSVImportError(Exception):
    """Raised when CSV import fails or validation does not pass."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_swedish_amount(value: str, decimal_sep: str = ",", thousands_sep: str = " ") -> int:
    """Parse ett belopp i svensk notation till öre (int).

    Hanterar:
    - Tusentalsavgränsare (mellanrum, t.ex. "31 036,00")
    - Decimalkomma ("31 036,00" → 3103600 öre)
    - Negativt belopp ("-87 768,00" → -8776800 öre)
    - Tom sträng → 0

    Raises:
        CSVImportError: Om strängen inte kan tolkas som ett belopp.
    """
    v = value.strip()
    if not v:
        return 0

    negative = v.startswith("-")
    v = v.lstrip("-").strip()

    # Ta bort tusentalsavgränsare och ersätt decimalkomma med punkt
    v = v.replace(thousands_sep, "").replace(decimal_sep, ".")

    try:
        kr = float(v)
    except ValueError as exc:
        raise CSVImportError(f"Kan inte parsa belopp: {value!r}") from exc

    oren = round(kr * 100)
    return -oren if negative else oren


def _parse_date(value: str, fmt: str = "%Y-%m-%d") -> date:
    """Parse datumstring med givet format.

    Raises:
        CSVImportError: Om strängen inte matchar formatet.
    """
    from datetime import datetime

    try:
        return datetime.strptime(value.strip(), fmt).date()
    except ValueError as exc:
        raise CSVImportError(f"Ogiltigt datum {value!r} (förväntat format {fmt})") from exc


def _detect_encoding(filepath: str) -> str:
    """Detektera teckenkodning — kontrollera BOM, sedan chardet."""
    raw = Path(filepath).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    result = chardet.detect(raw[:4096])
    return result.get("encoding") or "utf-8"


# ---------------------------------------------------------------------------
# Main importer
# ---------------------------------------------------------------------------

def import_bank_csv(
    filepath: str,
    bank_format: str = "seb",
    config: dict | None = None,
) -> list[BankTransaction]:
    """Importerar bankutdrag från CSV.

    Args:
        filepath: Sökväg till CSV-fil.
        bank_format: Bankformat att använda (default: "seb").
        config: Konfiguration med kolumnmappning etc.  Om None, används SEB-default.

    Returns:
        Lista av BankTransaction, en per datarad i CSV:en.

    Raises:
        CSVImportError: Om filen inte kan parsas eller saknar förväntade kolumner.
    """
    raw = Path(filepath).read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()

    # Välj formatkonfiguration
    if config is not None:
        fmt_cfg = config.get("csv_formats", {}).get(bank_format, _DEFAULT_SEB_CONFIG)
    else:
        fmt_cfg = _DEFAULT_SEB_CONFIG

    encoding = fmt_cfg.get("encoding") or _detect_encoding(filepath)
    delimiter = fmt_cfg.get("delimiter", ";")
    col_map: dict = fmt_cfg["columns"]
    date_fmt: str = fmt_cfg.get("date_format", "%Y-%m-%d")
    dec_sep: str = fmt_cfg.get("decimal_separator", ",")
    thou_sep: str = fmt_cfg.get("thousands_separator", " ")

    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        # Fallback
        encoding = _detect_encoding(filepath)
        text = raw.decode(encoding, errors="replace")
        logger.warning("Fallback-kodning %s för %s", encoding, filepath)

    lines = text.splitlines()
    if not lines:
        raise CSVImportError(f"Tom fil: {filepath}")

    # Parsa header (rad 1 = index 0)
    header = [h.strip() for h in lines[0].split(delimiter)]

    # Validera att alla förväntade kolumner finns
    missing = [v for v in col_map.values() if v not in header]
    if missing:
        raise CSVImportError(
            f"Saknade kolumner i {filepath}: {missing}\n"
            f"Hittade kolumner: {header}"
        )

    # Bygg kolumnindex-dict
    idx = {key: header.index(col_name) for key, col_name in col_map.items()}

    # Räkna förväntade datarader (alla icke-tomma rader efter headern)
    data_lines = [ln for ln in lines[1:] if ln.strip()]
    expected_count = len(data_lines)
    logger.info("Förväntar %d datarader i %s", expected_count, filepath)

    transactions: list[BankTransaction] = []
    prev_balance: int | None = None

    for line_offset, raw_line in enumerate(data_lines, start=2):  # rad 1 = header
        cols = raw_line.split(delimiter)
        # Paddning om sista kolumner är tomma
        while len(cols) < len(header):
            cols.append("")

        try:
            booking_date = _parse_date(cols[idx["booking_date"]], date_fmt)
            value_date = _parse_date(cols[idx["value_date"]], date_fmt)
            tx_text = cols[idx["text"]].strip()
            tx_type = cols[idx["transaction_type"]].strip()

            deposit_str = cols[idx["deposits"]].strip()
            withdrawal_str = cols[idx["withdrawals"]].strip()
            balance_str = cols[idx["balance"]].strip()

            deposit = _parse_swedish_amount(deposit_str, dec_sep, thou_sep)
            withdrawal = _parse_swedish_amount(withdrawal_str, dec_sep, thou_sep)
            balance = _parse_swedish_amount(balance_str, dec_sep, thou_sep)

            # Belopp: insättning om den finns, annars uttag (uttag är redan negativt)
            if deposit_str:
                amount = deposit
            else:
                amount = withdrawal

            # Validera belopp
            if deposit_str and withdrawal_str:
                logger.warning(
                    "Rad %d: Både insättning och uttag är ifyllda — använder insättning",
                    line_offset,
                )

            # Löpande saldo-kontroll (filen är i omvänd kronologisk ordning)
            if prev_balance is not None:
                expected_balance = prev_balance - prev_amount  # type: ignore[name-defined]
                diff = abs(balance - expected_balance)
                if diff > 1:  # ±1 öre tolerans
                    logger.warning(
                        "Rad %d: Saldoavvikelse %d öre (förväntat %d, fick %d)",
                        line_offset,
                        diff,
                        expected_balance,
                        balance,
                    )

            transactions.append(
                BankTransaction(
                    booking_date=booking_date,
                    value_date=value_date,
                    text=tx_text,
                    transaction_type=tx_type,
                    amount=amount,
                    running_balance=balance,
                    source_file=str(Path(filepath).resolve()),
                    source_row=line_offset,
                )
            )
            prev_balance = balance
            prev_amount = amount  # type: ignore[name-defined]

        except CSVImportError as exc:
            raise CSVImportError(f"Rad {line_offset}: {exc}") from exc

    # Nolltolerans-kontroll
    if len(transactions) != expected_count:
        raise CSVImportError(
            f"Antal parsade transaktioner ({len(transactions)}) "
            f"stämmer inte med antal datarader ({expected_count}) i {filepath}"
        )

    logger.info(
        "Importerad OK: %s | %d transaktioner | sha256=%s…",
        filepath,
        len(transactions),
        checksum[:12],
    )
    return transactions
