"""Dataklasser för forensisk bokföringsanalys."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class FiscalYear:
    """Representerar ett räkenskapsår."""

    year_index: int  # 0 = innevarande, -1 = föregående
    start_date: date
    end_date: date


@dataclass
class Account:
    """Ett konto i kontoplanen."""

    number: str
    name: str
    account_type: str  # T=tillgång, S=skuld, K=kostnad, I=intäkt
    sru_code: str | None = None


@dataclass
class Dimension:
    """En dimension (t.ex. Resultatenhet, Projekt)."""

    number: int
    name: str
    objects: dict[str, str] = field(default_factory=dict)  # objekt-id → objektnamn


@dataclass
class Transaction:
    """En rad i en verifikation."""

    account: str
    amount: int  # belopp i ören (heltal — aldrig float)
    objects: list[tuple[int, str]] = field(default_factory=list)  # (dimension-nr, objekt-id)
    text: str | None = None
    quantity: float | None = None


@dataclass
class Voucher:
    """En verifikation."""

    series: str
    number: int
    date: date
    text: str
    transactions: list[Transaction] = field(default_factory=list)
    registration_date: date | None = None


@dataclass
class Company:
    """Representerar ett bolag med all dess bokföringsdata."""

    org_nr: str  # format "NNNNNN-NNNN"
    name: str
    role: str  # holding/dotterbolag/intressebolag/fusionerat
    fiscal_years: list[FiscalYear] = field(default_factory=list)
    chart_of_accounts: dict[str, Account] = field(default_factory=dict)  # kontonr → Account
    vouchers: list[Voucher] = field(default_factory=list)
    opening_balances: dict[str, Decimal] = field(default_factory=dict)  # kontonr → IB
    closing_balances: dict[str, Decimal] = field(default_factory=dict)  # kontonr → UB
    results: dict[str, Decimal] = field(default_factory=dict)  # kontonr → resultat
    dimensions: dict[int, Dimension] = field(default_factory=dict)
    source_file: str = ""
    source_checksum: str = ""


@dataclass
class BankTransaction:
    """En rad från ett bankutdrag."""

    booking_date: date
    value_date: date
    text: str
    transaction_type: str  # Bankgirobetalning, Lön, Annan etc.
    amount: int  # belopp i ören (positivt = insättning, negativt = uttag)
    running_balance: int  # bokfört saldo i ören
    source_file: str
    source_row: int  # radnummer i CSV (1-indexerat, header = rad 1)


@dataclass
class Finding:
    """Ett fynd från en analysmodul."""

    finding_id: str  # format: "MOD_A_001"
    module: str
    companies: list[str]  # berörda bolag, org_nr
    risk_level: str  # CRITICAL/HIGH/MEDIUM/LOW/INFO
    category: str
    summary: str
    details: dict = field(default_factory=dict)
    ai_reasoning: str | None = None
    status: str = "open"  # open/investigated/confirmed/dismissed
