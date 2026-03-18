"""Modul G: Verksamhetsrelevans.

Identifierar motparter i bokföring/banktransaktioner och bedömer om deras
verksamhet är förenlig med bolagets kärnverksamhet (IT).

Arkitektur:
  LookupProvider    Protocol     Slår upp bolagsinformation
  MockLookupProvider             Används i tester och vid nätverksfel
  AllabolagetProvider            Slår upp mot allabolag.se med JSON-cache

BusinessRelevanceModule tar `lookup_provider` och `ai_client` som
konstruktorargument (mock-first design).

Fynd:
  unknown_counterparty  MEDIUM   Motpart ej identifierbar
  unrelated_business    MEDIUM   Verksamhet utan IT-koppling
  private_supplier      HIGH     Betalning till enskild firma/privatperson
  luxury_consumption    MEDIUM   Representation/lyxkonsumtion
  geographic_anomaly    MEDIUM   Oväntat geografiskt ursprung
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, Protocol, runtime_checkable

from src.core.models import BankTransaction, Company, Finding
from src.modules.base_module import AnalysisModule

logger = logging.getLogger(__name__)

# SNI-koder som räknas som IT-relaterade
_IT_SNI_PREFIXES = ("58", "59", "60", "61", "62", "63", "26", "95")
_IT_KEYWORDS = (
    "it", "data", "mjukvara", "software", "tech", "digital",
    "system", "drift", "nätverk", "webb", "internet", "program",
)
_LUXURY_KEYWORDS = (
    "spa", "hotell", "restaurang", "golf", "båt", "flyg business",
    "lyxig", "lyx", "representation", "konferens",
)
_PRIVATE_SUFFIXES = {"ab", "hb", "kb", "handelsbolag", "aktiebolag", "kommanditbolag"}

# Bolagsformer som antas vara enskilda firmor / privatpersoner
_CORPORATE_INDICATORS = re.compile(
    r"\b(AB|HB|KB|Aktiebolag|Handelsbolag|Kommanditbolag|AB\.?)\b",
    re.IGNORECASE,
)

# Generiska bokföringsord som inte är motpartsnamn
_GENERIC_TEXTS = frozenset({
    "lön", "loner", "löner", "löneutbetalning", "salary", "wages",
    "moms", "mervärdesskatt", "vat", "skatt", "skatter",
    "hyra", "kontorshyra", "lokal", "lokalhyra",
    "el", "el och värme", "el/värme", "värme", "vatten",
    "telefon", "mobiltelefon", "internet", "bredband",
    "försäkring", "försäkringar",
    "avskrivning", "avskrivningar", "amortering",
    "ränta", "räntor", "bankränta", "dröjsmålsränta",
    "bankavgift", "bankavgifter", "kontoavgift",
    "porto", "frakt", "fraktkostnad",
    "inventarier", "dator", "datorer",
    "representation", "personalrepresentation",
    "bokföring", "revision", "revisionskostnad", "redovisning",
    "swish", "bankgiro", "plusgiro", "autogiro",
    "kreditkort", "visa", "mastercard", "kortköp",
    "lönesammanställning", "löneberedning",
    "deposition", "depositionsavgift",
    "reklamation", "kreditnota", "kredit",
    "övrigt", "diverse", "övrigt levrant", "blandade kostnader",
    "ingående saldo", "utgående saldo", "ingående balans",
})

# Regex för BG/PG-nummer och referensnummer som inte är företagsnamn
_REFERENCE_PATTERNS = re.compile(
    r"^("
    r"bg\s*[\d\-\s]+|"          # BG 1234-5678
    r"pg\s*[\d\-\s]+|"          # PG 12345-6
    r"\d{3,5}[-\s]?\d{4,6}|"    # 12345-6789 (BG/PG format)
    r"#\d+|"                     # #12345 (referensnummer)
    r"\d{5,}|"                   # Rent numerisk sträng (5+ siffror)
    r"ocr\s*\d+|"               # OCR-nummer
    r"faktura\s*\d+|"            # Faktura nr
    r"fakt\s*\d+|"              # Förkortat faktura nr
    r"ref\s*[:.]?\s*\d+"         # REF: 12345
    r")",
    re.IGNORECASE,
)


def _is_likely_company_name(text: str) -> bool:
    """Returnerar True om texten troligtvis är ett motpartsnamn (inte generisk text)."""
    if not text or len(text) < 3:
        return False
    # Generiska bokföringsord
    if text.lower() in _GENERIC_TEXTS:
        return False
    # BG/PG-nummer och referensnummer
    if _REFERENCE_PATTERNS.match(text.strip()):
        return False
    # Rent numerisk (eventuellt med bindestreck/mellanslag) — inte ett namn
    stripped = text.replace("-", "").replace(" ", "")
    if stripped.isdigit():
        return False
    # Mycket korta texter (1-2 ord, inga versaler) är troligen inte företagsnamn
    if len(text) < 5 and not any(c.isupper() for c in text):
        return False
    return True


# ---------------------------------------------------------------------------
# Datatyper
# ---------------------------------------------------------------------------

class CompanyInfo(NamedTuple):
    """Information om ett uppslaget bolag."""
    org_nr: str | None
    name: str
    sni_code: str | None
    description: str | None
    status: str  # "active" / "inactive" / "unknown"


@runtime_checkable
class LookupProvider(Protocol):
    """Protokoll för bolagsuppslagning."""
    def lookup(self, company_name: str) -> CompanyInfo: ...


# ---------------------------------------------------------------------------
# Mock-provider
# ---------------------------------------------------------------------------

class MockLookupProvider:
    """Används i tester och som fallback vid nätverksfel."""

    def __init__(self, data: dict[str, CompanyInfo]) -> None:
        self.data = data
        self.call_log: list[str] = []

    def lookup(self, company_name: str) -> CompanyInfo:
        self.call_log.append(company_name)
        return self.data.get(
            company_name,
            CompanyInfo(None, company_name, None, None, "unknown"),
        )


# ---------------------------------------------------------------------------
# Allabolag-provider
# ---------------------------------------------------------------------------

class AllabolagetProvider:
    """Slår upp bolagsinformation på allabolag.se med JSON-filcache.

    Args:
        cache_file: Sökväg till cache-filen. Standard: ``./cache/allabolag_cache.json``.
        request_delay_s: Sekunder att vänta mellan anrop (rate limiting).
        cache_max_age_days: Antal dagar innan en cache-post räknas som inaktuell.
    """

    DEFAULT_CACHE_FILE = "./cache/allabolag_cache.json"
    BASE_URL = "https://www.allabolag.se/what/{name}/1"

    def __init__(
        self,
        cache_file: str | None = None,
        request_delay_s: float = 1.0,
        cache_max_age_days: int = 30,
    ) -> None:
        self._cache_file = Path(cache_file or self.DEFAULT_CACHE_FILE)
        self._delay = request_delay_s
        self._max_age = cache_max_age_days
        self._cache: dict[str, dict] = self._load_cache()

    # ------------------------------------------------------------------

    def _load_cache(self) -> dict[str, dict]:
        try:
            if self._cache_file.exists():
                return json.loads(self._cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Kan inte läsa cache %s: %s", self._cache_file, exc)
        return {}

    def _save_cache(self) -> None:
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Kan inte spara cache %s: %s", self._cache_file, exc)

    def _is_fresh(self, entry: dict) -> bool:
        try:
            cached_at = datetime.fromisoformat(entry["cached_at"])
            return datetime.now() - cached_at <= timedelta(days=self._max_age)
        except (KeyError, ValueError):
            return False

    def _fetch(self, company_name: str) -> CompanyInfo:
        """Hämtar information från allabolag.se."""
        encoded = urllib.parse.quote(company_name)
        url = self.BASE_URL.format(name=encoded)
        logger.debug("HTTP GET %s", url)
        time.sleep(self._delay)

        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (forensic-analyzer)"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("allabolag.se ej nåbar för '%s': %s", company_name, exc)
            return CompanyInfo(None, company_name, None, None, "unknown")

        # Parsa <meta name="description" content="...">
        desc_match = re.search(
            r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']',
            html, re.IGNORECASE,
        )
        description = desc_match.group(1).strip() if desc_match else None

        # Parsa <title>...</title>
        title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else company_name

        # Heuristik: aktiv om "aktiebolag" förekommer i title/description
        status = "unknown"
        combined = f"{title} {description or ''}".lower()
        if "aktiebolag" in combined or " ab " in combined:
            status = "active"

        return CompanyInfo(
            org_nr=None,
            name=company_name,
            sni_code=None,
            description=description,
            status=status,
        )

    def lookup(self, company_name: str) -> CompanyInfo:
        entry = self._cache.get(company_name)
        if entry and self._is_fresh(entry):
            logger.debug("Cache-träff för '%s'", company_name)
            return CompanyInfo(
                org_nr=entry.get("org_nr"),
                name=company_name,
                sni_code=entry.get("sni_code"),
                description=entry.get("description"),
                status=entry.get("status", "unknown"),
            )

        info = self._fetch(company_name)
        self._cache[company_name] = {
            "org_nr": info.org_nr,
            "sni_code": info.sni_code,
            "description": info.description,
            "status": info.status,
            "cached_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save_cache()
        return info


# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """Normaliserar företagsnamn för uppslagning."""
    name = name.strip()
    # Ta bort bolagsformsuffix
    name = re.sub(r"\s+(AB|HB|KB|Aktiebolag|Handelsbolag|Kommanditbolag)\.?\s*$",
                  "", name, flags=re.IGNORECASE).strip()
    return name


def _is_it_related(info: CompanyInfo) -> bool:
    """Returnerar True om bolagets verksamhet verkar IT-relaterad."""
    if info.sni_code:
        for prefix in _IT_SNI_PREFIXES:
            if info.sni_code.startswith(prefix):
                return True
    if info.description:
        desc_lower = info.description.lower()
        if any(kw in desc_lower for kw in _IT_KEYWORDS):
            return True
    return False


def _is_private(info: CompanyInfo) -> bool:
    """Returnerar True om motparten verkar vara en enskild firma/privatperson."""
    if not _CORPORATE_INDICATORS.search(info.name):
        return True
    if info.sni_code == "9999":
        return True
    return False


def _extract_counterpart_names(
    company: Company,
    bank_txs: list[BankTransaction],
) -> list[str]:
    """Extraherar troliga motpartsnamn från verifikationstexter och banktransaktionstexter.

    Filtrerar bort generiska bokföringsord, BG/PG-nummer och referensnummer.
    """
    names: set[str] = set()
    for v in company.vouchers:
        text = v.text.strip()
        if _is_likely_company_name(text):
            names.add(text)
        for t in v.transactions:
            if t.text and _is_likely_company_name(t.text.strip()):
                names.add(t.text.strip())
    for tx in bank_txs:
        text = tx.text.strip()
        if _is_likely_company_name(text):
            names.add(text)
    return list(names)


# ---------------------------------------------------------------------------
# Modulklass
# ---------------------------------------------------------------------------

class BusinessRelevanceModule(AnalysisModule):
    """Modul G: Verksamhetsrelevans."""

    def __init__(
        self,
        lookup_provider: LookupProvider | None = None,
        ai_client: Any | None = None,
    ) -> None:
        super().__init__()
        if lookup_provider is None:
            try:
                lookup_provider = AllabolagetProvider()
            except Exception as exc:  # pragma: no cover
                logger.warning("AllabolagetProvider ej tillgänglig: %s", exc)
                lookup_provider = MockLookupProvider({})
        self.lookup_provider: LookupProvider = lookup_provider
        self.ai_client = ai_client

    @property
    def name(self) -> str:
        return "business_relevance"

    @property
    def description(self) -> str:
        return (
            "Identifierar motparter i transaktioner och bedömer om deras "
            "verksamhet är förenlig med IT-bolagets kärnverksamhet."
        )

    def analyze(
        self,
        companies: list[Company],
        bank_transactions: dict[str, list[BankTransaction]],
        config: dict,
    ) -> list[Finding]:
        findings: list[Finding] = []
        for company in companies:
            bank_txs = bank_transactions.get(company.org_nr, [])
            findings.extend(self._analyze_company(company, bank_txs, config))
        return findings

    # ------------------------------------------------------------------

    def _analyze_company(
        self,
        company: Company,
        bank_txs: list[BankTransaction],
        config: dict,
    ) -> list[Finding]:
        findings: list[Finding] = []
        names = _extract_counterpart_names(company, bank_txs)
        seen: set[str] = set()

        for raw_name in names:
            normalized = _normalize_name(raw_name)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)

            info = self.lookup_provider.lookup(normalized)

            if info.status == "unknown":
                findings.append(self._create_finding(
                    category="unknown_counterparty",
                    risk_level="MEDIUM",
                    summary=(
                        f"{company.org_nr}: Motpart '{normalized}' "
                        f"kan inte identifieras"
                    ),
                    companies=[company.org_nr],
                    details={
                        "org_nr": company.org_nr,
                        "counterparty_name": normalized,
                        "raw_name": raw_name,
                        "lookup_status": "unknown",
                    },
                ))
                continue

            if _is_private(info):
                findings.append(self._create_finding(
                    category="private_supplier",
                    risk_level="HIGH",
                    summary=(
                        f"{company.org_nr}: Möjlig betalning till enskild firma "
                        f"'{normalized}'"
                    ),
                    companies=[company.org_nr],
                    details={
                        "org_nr": company.org_nr,
                        "counterparty_name": normalized,
                        "company_info": info._asdict(),
                    },
                ))
                continue

            if _is_it_related(info):
                continue  # Förenlig med kärnverksamheten

            # AI-bedömning om klient är satt
            reasoning: str = ""
            relevant: bool | None = None
            if self.ai_client is not None:
                relevant, reasoning = self._ask_claude(info, [raw_name])

            if relevant is False or (relevant is None and not _is_it_related(info)):
                # Kontrollera luxuskonsumtion
                category = "unrelated_business"
                risk = "MEDIUM"
                if info.description:
                    desc_lower = info.description.lower()
                    if any(kw in desc_lower for kw in _LUXURY_KEYWORDS):
                        category = "luxury_consumption"

                findings.append(self._create_finding(
                    category=category,
                    risk_level=risk,
                    summary=(
                        f"{company.org_nr}: Motpart '{normalized}' verkar sakna "
                        f"IT-koppling ('{info.description or info.sni_code}')"
                    ),
                    companies=[company.org_nr],
                    details={
                        "org_nr": company.org_nr,
                        "counterparty_name": normalized,
                        "company_info": info._asdict(),
                        "ai_relevant": relevant,
                        "ai_reasoning": reasoning,
                    },
                ))
        return findings

    def _ask_claude(
        self,
        info: CompanyInfo,
        sample_texts: list[str],
    ) -> tuple[bool | None, str]:
        """Frågar Claude om motpartens verksamhet är förenlig med IT-verksamhet."""
        prompt = (
            f"Motpart: {info.name}\n"
            f"Verksamhetsbeskrivning: {info.description or 'Okänd'}\n"
            f"SNI-kod: {info.sni_code or 'Okänd'}\n"
            f"Exempeltransaktioner: {', '.join(sample_texts[:3])}\n\n"
            "Är denna transaktion förenlig med en IT-verksamhet som säljer "
            "produkter och tjänster till företag? "
            "Svara med JSON: "
            '{\"relevant\": true/false/null, \"reasoning\": \"...\"}'
        )
        try:
            response = self.ai_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            data = json.loads(text)
            return data.get("relevant"), data.get("reasoning", "")
        except Exception as exc:
            logger.warning("Claude API-fel för '%s': %s", info.name, exc)
            return None, ""
