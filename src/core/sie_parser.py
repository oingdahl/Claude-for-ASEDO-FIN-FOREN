"""Parser för SIE4-filer.

Nolltolerans för dataförlust: parsningen avslutas med SIEParseError
om antal verifikationer, transaktioner eller konton inte stämmer exakt.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import chardet

from src.core.models import Account, Company, Dimension, FiscalYear, Transaction, Voucher

logger = logging.getLogger(__name__)


class SIEParseError(Exception):
    """Raised when SIE4 parsing fails or validation does not pass."""


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------

def _detect_encoding(filepath: str) -> str:
    """Determine text encoding from raw bytes of a SIE4 file.

    Priority:
      1. UTF-8 BOM  →  utf-8-sig
      2. #FORMAT PC8  →  cp437
      3. chardet (confidence > 0.7)
      4. fallback: cp437
    """
    raw = Path(filepath).read_bytes()

    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"

    # #FORMAT is in the ASCII-safe portion of the file
    header = raw[:512].decode("ascii", errors="replace")
    for line in header.splitlines():
        tokens = line.split()
        if tokens and tokens[0].upper() == "#FORMAT":
            if len(tokens) > 1 and tokens[1].upper() == "PC8":
                return "cp437"
            break

    result = chardet.detect(raw[:4096])
    if result.get("encoding") and (result.get("confidence") or 0) > 0.7:
        return result["encoding"]

    return "cp437"


def _has_mojibake(text: str) -> bool:
    """Return True if text contains common UTF-8/Latin-1 mojibake patterns."""
    mojibake = ["Ã¶", "Ã¤", "Ã¥", "Ã–", "Ã„", "Ã…"]
    return any(m in text for m in mojibake)


def _read_file(filepath: str) -> tuple[str, bytes]:
    """Read file, return (decoded_text, raw_bytes).

    Tries the auto-detected encoding first, then Latin-1 and UTF-8 as
    fallbacks.  If Swedish characters appear as mojibake, the next
    candidate is tried.
    """
    raw = Path(filepath).read_bytes()
    primary = _detect_encoding(filepath)

    for enc in [primary, "latin-1", "utf-8"]:
        try:
            text = raw.decode(enc)
            if not _has_mojibake(text):
                logger.debug("Decoded %s with encoding=%s", filepath, enc)
                return text, raw
        except (UnicodeDecodeError, LookupError):
            continue

    # Last resort: chardet
    detected = chardet.detect(raw).get("encoding") or "latin-1"
    logger.warning("Fallback to chardet encoding=%s for %s", detected, filepath)
    return raw.decode(detected, errors="replace"), raw


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

def _tokenize(line: str) -> list[str]:
    """Split a SIE4 line into tokens.

    Rules:
    - Whitespace is the delimiter.
    - ``"..."`` → quoted string (quotes stripped, spaces preserved).
    - ``{...}`` → object-list token (braces kept, treated as one token).
    - Everything else → plain whitespace-delimited token.
    """
    tokens: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch.isspace():
            i += 1
        elif ch == '"':
            # Quoted string – find closing quote
            j = i + 1
            while j < n and line[j] != '"':
                j += 1
            tokens.append(line[i + 1 : j])
            i = j + 1
        elif ch == "{":
            # Object list – find closing brace
            j = i + 1
            while j < n and line[j] != "}":
                j += 1
            tokens.append(line[i : j + 1])  # include braces
            i = j + 1
        else:
            # Plain token
            j = i
            while j < n and not line[j].isspace():
                j += 1
            tokens.append(line[i:j])
            i = j
    return tokens


# ---------------------------------------------------------------------------
# Value parsers
# ---------------------------------------------------------------------------

def _parse_date(value: str) -> date:
    """Parse YYYYMMDD date string.

    Raises SIEParseError on invalid input.
    """
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except (ValueError, IndexError) as exc:
        raise SIEParseError(f"Ogiltigt datum: {value!r}") from exc


def _parse_amount(value: str) -> int:
    """Parse an amount string (decimal, period as separator) to öre (int).

    Validates that precision loss is ≤ 0.004 kr.

    Raises SIEParseError on invalid input.
    """
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise SIEParseError(f"Ogiltigt belopp: {value!r}") from exc

    oren_exact = d * 100
    oren_int = int(oren_exact.quantize(Decimal("1")))
    precision_loss_kr = abs(oren_exact - oren_int) / 100
    if precision_loss_kr > Decimal("0.004"):
        raise SIEParseError(
            f"Belopp {value!r} tappar mer än 0.004 kr vid konvertering "
            f"till ören (förlust={precision_loss_kr} kr)"
        )
    return oren_int


def _parse_objects(obj_token: str) -> list[tuple[int, str]]:
    """Parse ``{dim1 obj1 dim2 obj2}`` to a list of (dimension, object-id) tuples.

    Returns an empty list for ``{}``.
    """
    content = obj_token.strip("{}").strip()
    if not content:
        return []
    parts = content.split()
    result: list[tuple[int, str]] = []
    for i in range(0, len(parts) - 1, 2):
        try:
            result.append((int(parts[i]), parts[i + 1]))
        except (ValueError, IndexError) as exc:
            logger.warning("Kan inte parsa objektpar vid index %d: %s", i, exc)
    return result


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_sie4(filepath: str) -> Company:
    """Parsar en SIE4-fil och returnerar ett Company-objekt.

    Args:
        filepath: Sökväg till SIE4-filen (.SE eller .SIE).

    Returns:
        Populerat Company-objekt.

    Raises:
        SIEParseError: Om filen inte kan parsas eller inte validerar.
    """
    text, raw = _read_file(filepath)
    checksum = hashlib.sha256(raw).hexdigest()

    lines = text.splitlines()

    # Pre-count expected records from source (for zero-loss validation)
    expected_ver_count = sum(
        1 for ln in lines if re.match(r"^\s*#VER\b", ln)
    )
    expected_trans_count = sum(
        1 for ln in lines if re.match(r"^\s*#TRANS\b", ln)
    )
    expected_konto_count = sum(
        1 for ln in lines if re.match(r"^\s*#KONTO\b", ln)
    )
    logger.info(
        "Förväntade poster: %d #VER, %d #TRANS, %d #KONTO",
        expected_ver_count,
        expected_trans_count,
        expected_konto_count,
    )

    company = Company(
        org_nr="",
        name="",
        role="",
        source_file=str(Path(filepath).resolve()),
        source_checksum=checksum,
    )

    current_voucher: Voucher | None = None
    in_voucher_block = False

    for line_no, raw_line in enumerate(lines, 1):
        stripped = raw_line.strip()

        # Skip blank lines and comments
        if not stripped or stripped.startswith(";"):
            continue

        # Block delimiters
        if stripped == "{":
            in_voucher_block = True
            continue
        if stripped == "}":
            if current_voucher is not None:
                company.vouchers.append(current_voucher)
                current_voucher = None
            in_voucher_block = False
            continue

        if not stripped.startswith("#"):
            continue

        tokens = _tokenize(stripped)
        if not tokens:
            continue

        directive = tokens[0].upper()
        args = tokens[1:]

        # --- Metadata ----------------------------------------------------------
        if directive == "#FNAMN":
            company.name = args[0] if args else ""

        elif directive == "#ORGNR":
            company.org_nr = args[0] if args else ""

        elif directive == "#RAR":
            if len(args) >= 3:
                try:
                    yr_idx = int(args[0])
                    start = _parse_date(args[1])
                    end = _parse_date(args[2])
                    company.fiscal_years.append(FiscalYear(yr_idx, start, end))
                except (SIEParseError, ValueError) as exc:
                    logger.warning("Rad %d: #RAR-fel: %s", line_no, exc)

        # --- Chart of accounts -------------------------------------------------
        elif directive == "#KONTO":
            if len(args) >= 2:
                nr, name = args[0], args[1]
                if nr not in company.chart_of_accounts:
                    company.chart_of_accounts[nr] = Account(nr, name, "")
                else:
                    company.chart_of_accounts[nr].name = name

        elif directive == "#KTYP":
            if len(args) >= 2:
                nr, ktyp = args[0], args[1]
                if nr not in company.chart_of_accounts:
                    company.chart_of_accounts[nr] = Account(nr, "", ktyp)
                else:
                    company.chart_of_accounts[nr].account_type = ktyp

        elif directive == "#SRU":
            if len(args) >= 2:
                nr, sru = args[0], args[1]
                if nr in company.chart_of_accounts:
                    company.chart_of_accounts[nr].sru_code = sru

        # --- Dimensions --------------------------------------------------------
        elif directive == "#DIM":
            if len(args) >= 2:
                try:
                    dim_nr = int(args[0])
                    dim_name = args[1]
                    if dim_nr not in company.dimensions:
                        company.dimensions[dim_nr] = Dimension(dim_nr, dim_name)
                    else:
                        company.dimensions[dim_nr].name = dim_name
                except ValueError as exc:
                    logger.warning("Rad %d: #DIM-fel: %s", line_no, exc)

        elif directive == "#OBJEKT":
            if len(args) >= 3:
                try:
                    dim_nr = int(args[0])
                    obj_id = args[1]
                    obj_name = args[2]
                    if dim_nr not in company.dimensions:
                        company.dimensions[dim_nr] = Dimension(dim_nr, "")
                    company.dimensions[dim_nr].objects[obj_id] = obj_name
                except ValueError as exc:
                    logger.warning("Rad %d: #OBJEKT-fel: %s", line_no, exc)

        # --- Balances ----------------------------------------------------------
        elif directive in ("#IB", "#UB", "#RES"):
            if len(args) >= 3:
                try:
                    yr_idx = int(args[0])
                    acc_nr = args[1]
                    amount = Decimal(args[2])
                    if yr_idx == 0:  # innevarande räkenskapsår
                        if directive == "#IB":
                            company.opening_balances[acc_nr] = amount
                        elif directive == "#UB":
                            company.closing_balances[acc_nr] = amount
                        elif directive == "#RES":
                            company.results[acc_nr] = amount
                except (ValueError, InvalidOperation) as exc:
                    logger.warning("Rad %d: %s-fel: %s", line_no, directive, exc)

        # --- Vouchers ----------------------------------------------------------
        elif directive == "#VER":
            # #VER serie nummer datum [text] [regdatum]
            if len(args) >= 3:
                try:
                    series = args[0]
                    number = int(args[1])
                    ver_date = _parse_date(args[2])
                    ver_text = args[3] if len(args) > 3 else ""
                    reg_date = _parse_date(args[4]) if len(args) > 4 else None
                    current_voucher = Voucher(
                        series=series,
                        number=number,
                        date=ver_date,
                        text=ver_text,
                        registration_date=reg_date,
                    )
                except (SIEParseError, ValueError) as exc:
                    raise SIEParseError(
                        f"Rad {line_no}: Kan inte parsa #VER: {exc}"
                    ) from exc

        # --- Transactions (inside voucher block) -------------------------------
        elif directive == "#TRANS" and in_voucher_block and current_voucher is not None:
            # #TRANS kontonr {objektlista} belopp [transtext] [kvantitet]
            if len(args) >= 3:
                try:
                    acc_nr = args[0]
                    objects = _parse_objects(args[1])
                    amount = _parse_amount(args[2])
                    trans_text: str | None = args[3] if len(args) > 3 else None
                    quantity: float | None = (
                        float(args[4]) if len(args) > 4 and args[4] else None
                    )
                    current_voucher.transactions.append(
                        Transaction(
                            account=acc_nr,
                            amount=amount,
                            objects=objects,
                            text=trans_text,
                            quantity=quantity,
                        )
                    )
                except (SIEParseError, ValueError) as exc:
                    raise SIEParseError(
                        f"Rad {line_no}: Kan inte parsa #TRANS: {exc}"
                    ) from exc

    # ---------------------------------------------------------------------------
    # Validation
    # ---------------------------------------------------------------------------
    errors: list[str] = []

    actual_ver = len(company.vouchers)
    actual_trans = sum(len(v.transactions) for v in company.vouchers)
    actual_konto = len(company.chart_of_accounts)

    if actual_ver != expected_ver_count:
        errors.append(
            f"Verifikationsantal: förväntat {expected_ver_count}, parsade {actual_ver}"
        )
    if actual_trans != expected_trans_count:
        errors.append(
            f"Transaktionsantal: förväntat {expected_trans_count}, parsade {actual_trans}"
        )
    if actual_konto != expected_konto_count:
        errors.append(
            f"Kontoplanantal: förväntat {expected_konto_count}, parsade {actual_konto}"
        )

    for v in company.vouchers:
        total = sum(t.amount for t in v.transactions)
        if abs(total) > 1:  # ±1 öre tolerans
            errors.append(
                f"Verifikation {v.series}{v.number} ({v.date}) balanserar inte: "
                f"summa={total} öre"
            )

    if errors:
        msg = "SIE4-validering misslyckades:\n" + "\n".join(
            f"  • {e}" for e in errors
        )
        logger.error(msg)
        raise SIEParseError(msg)

    logger.info(
        "Parsad OK: %s | %d ver | %d trans | %d konton | sha256=%s…",
        filepath,
        actual_ver,
        actual_trans,
        actual_konto,
        checksum[:12],
    )
    return company
