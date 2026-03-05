# PIPELINE PLAN: Forensisk Bokföringsanalys

## Instruktioner till Claude Code

Du ska bygga ett Python-baserat system för forensisk analys av svensk bokföringsdata (SIE4-filer) mot bankutdrag (CSV). Systemet ska identifiera avvikelser, röda flaggor och misstänkta mönster i en bolagssfär.

**Arbetsmetod:** Bygg steg för steg. Varje steg har en CHECKPOINT — stoppa och vänta på användarens bekräftelse innan du går vidare till nästa steg. Visa testresultaten vid varje checkpoint.

**Kodstandard:**
- Python 3.11+
- Använd type hints genomgående
- Docstrings på alla publika funktioner och klasser
- Inga beroenden utöver: pandas, numpy, scipy, anthropic, pyyaml, chardet
- All kod ska vara körbar och testad vid varje checkpoint
- Felhantering: fånga och logga — kasta aldrig bort data tyst

**Repo-struktur:**
```
forensic-analyzer/
├── README.md
├── requirements.txt
├── config/
│   ├── default_config.yaml      # Standardkonfiguration
│   └── wooz_config.yaml         # Koncernspecifik konfiguration
├── src/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── sie_parser.py        # SIE4-parser
│   │   ├── csv_importer.py      # Bankutdrag-import
│   │   ├── models.py            # Dataklasser
│   │   └── data_validator.py    # Datakvalitetsvalidering
│   ├── modules/
│   │   ├── __init__.py
│   │   ├── base_module.py       # Abstrakt basklass för moduler
│   │   ├── bank_matching.py     # Modul A: Bank vs Huvudbok
│   │   ├── intercompany.py      # Modul B: Koncernintern avstämning
│   │   ├── clearing_accounts.py # Modul C: Avräkningskonton
│   │   ├── amount_patterns.py   # Modul D: Beloppsmönster & Benford
│   │   ├── time_patterns.py     # Modul E: Tidsmönster
│   │   ├── salary_expenses.py   # Modul F: Löner & kreditkort
│   │   └── business_relevance.py # Modul G: Verksamhetsrelevans
│   ├── orchestrator/
│   │   ├── __init__.py
│   │   ├── engine.py            # AI-orkestrerare
│   │   └── prompts.py           # Promptmallar
│   └── output/
│       ├── __init__.py
│       ├── report_generator.py  # JSON-resultat och kontextfil
│       └── dashboard.py         # HTML-dashboard-generator
├── tests/
│   ├── __init__.py
│   ├── test_sie_parser.py
│   ├── test_csv_importer.py
│   ├── test_data_validator.py
│   └── test_modules/
│       ├── __init__.py
│       ├── test_bank_matching.py
│       ├── test_intercompany.py
│       └── ...
├── test_data/
│   ├── SIE4_Exempelfil.SE       # Exempelfil som medföljer
│   └── Sophone_2025_transar.csv # Exempelfil som medföljer
└── main.py                      # Huvudskript
```

---

## STEG 1: Projektstruktur och datamodell

### Vad ska byggas
1. Skapa hela repo-strukturen ovan med tomma `__init__.py`-filer
2. Skapa `requirements.txt` med: pandas, numpy, scipy, anthropic, pyyaml, chardet, pytest
3. Skapa `src/core/models.py` med följande dataklasser (använd `dataclasses`):

**Company** — representerar ett bolag:
- `org_nr: str` (format "NNNNNN-NNNN")
- `name: str`
- `role: str` (holding/dotterbolag/intressebolag/fusionerat)
- `fiscal_years: list[FiscalYear]`
- `chart_of_accounts: dict[str, Account]` (kontonr → Account)
- `vouchers: list[Voucher]`
- `opening_balances: dict[str, Decimal]` (kontonr → IB)
- `closing_balances: dict[str, Decimal]` (kontonr → UB)
- `results: dict[str, Decimal]` (kontonr → resultat)
- `dimensions: dict[int, Dimension]`
- `source_file: str`
- `source_checksum: str`

**FiscalYear:**
- `year_index: int` (0 = innevarande, -1 = föregående)
- `start_date: date`
- `end_date: date`

**Account:**
- `number: str`
- `name: str`
- `account_type: str` (T/S/K/I från #KTYP)
- `sru_code: str | None`

**Dimension:**
- `number: int`
- `name: str`
- `objects: dict[str, str]` (objekt-id → objektnamn)

**Voucher** — en verifikation:
- `series: str` (A, B, C etc)
- `number: int`
- `date: date` (verifikationsdatum)
- `text: str` (verifikationstext)
- `registration_date: date | None` (registreringsdatum)
- `transactions: list[Transaction]`

**Transaction** — en rad i en verifikation:
- `account: str` (kontonr)
- `amount: int` (belopp i ören — VIKTIGT: alla belopp lagras som heltal i ören)
- `objects: list[tuple[int, str]]` (dimension-nr, objekt-id)
- `text: str | None` (transaktionstext, om separat från verifikationen)
- `quantity: float | None`

**BankTransaction** — en rad från bankutdrag:
- `booking_date: date`
- `value_date: date`
- `text: str`
- `transaction_type: str` (Bankgirobetalning, Lön, Annan etc)
- `amount: int` (belopp i ören — positivt för insättning, negativt för uttag)
- `running_balance: int` (bokfört saldo i ören)
- `source_file: str`
- `source_row: int` (radnummer i CSV för spårbarhet)

**Finding** — ett fynd från en analysmodul:
- `finding_id: str` (unikt ID, format: "MOD_A_001")
- `module: str` (modulnamn)
- `companies: list[str]` (berörda bolag, org_nr)
- `risk_level: str` (CRITICAL/HIGH/MEDIUM/LOW/INFO)
- `category: str` (typ av fynd)
- `summary: str` (kort beskrivning)
- `details: dict` (underliggande data — transaktioner, belopp etc)
- `ai_reasoning: str | None` (fylls i av orkestreraren)
- `status: str` (open/investigated/confirmed/dismissed)

4. Skapa `config/default_config.yaml`:
```yaml
matching:
  amount_tolerance_bank_kr: 50        # ±50 kr för bank vs bok
  amount_tolerance_intercompany_kr: 500  # ±500 kr för koncernintern
  date_tolerance_days: 3               # ±3 dagar

bank_accounts: ["1910", "1920", "1930", "1940"]

intercompany_accounts:
  receivables: ["1660", "1661", "1662", "1663", "1664", "1665", "1666", "1667", "1668", "1669"]
  payables: ["2460", "2461", "2462", "2463", "2464", "2465", "2466", "2467", "2468", "2469"]

clearing_accounts: ["1630", "1631", "1632", "1633", "1634", "1635", "1680", "1681", "1685", "2393", "2890", "2891", "2892", "2893", "2894", "2895", "2896", "2897", "2898", "2899"]

salary_accounts:
  wages: ["7010", "7011", "7012", "7013", "7014", "7090"]
  social: ["7200", "7210", "7220", "7230", "7240", "7250", "7260", "7270", "7280", "7290", "7300", "7310", "7320", "7330", "7340", "7350", "7360", "7370", "7380", "7390", "7399"]

benford:
  min_sample_size: 50
  significance_level: 0.05

csv_formats:
  seb:
    encoding: "utf-8-sig"
    delimiter: ";"
    columns:
      booking_date: "Bokförd"
      value_date: "Valutadatum"
      text: "Text"
      transaction_type: "Typ"
      deposits: "Insättningar"
      withdrawals: "Uttag"
      balance: "Bokfört saldo"
    date_format: "%Y-%m-%d"
    decimal_separator: ","
    thousands_separator: " "
```

5. Skapa `config/wooz_config.yaml`:
```yaml
sphere:
  name: "Wooz-sfären"
  business_description: "Försäljning av IT-produkter och IT-tjänster till företag"

  companies:
    - org_nr: "XXXXXX-XXXX"  # Fylls i med riktiga orgnr
      name: "Wooz AB"
      role: "holding"
    - org_nr: "XXXXXX-XXXX"
      name: "Asedo Sverige AB"
      role: "dotterbolag"
    - org_nr: "XXXXXX-XXXX"
      name: "Asedo & Co AB"
      role: "dotterbolag"
    - org_nr: "XXXXXX-XXXX"
      name: "Sophone AB"
      role: "dotterbolag"
    - org_nr: "XXXXXX-XXXX"
      name: "Asedo AB"
      role: "fusionerat"
    - org_nr: "XXXXXX-XXXX"
      name: "Asedo Tech AB"
      role: "intressebolag"
    - org_nr: "XXXXXX-XXXX"
      name: "Asedo Connect AB"
      role: "fusionerat"
```

### Checkpoint 1
Kör: `python -c "from src.core.models import *; print('Alla modeller importerade OK')"`
Kör: `python -c "import yaml; c = yaml.safe_load(open('config/default_config.yaml')); print(f'Config laddad: {len(c)} nycklar')"`
Visa resultaten och vänta på bekräftelse.

---

## STEG 2: SIE4-parser

### Vad ska byggas
Skapa `src/core/sie_parser.py` — en robust parser för SIE4-filer.

**Kritiska krav:**
- NOLLTOLERANS FÖR DATAFÖRLUST. Ingen data får tyst försvinna i parsningen.
- Teckenkodning: Filen anger format via `#FORMAT`. "PC8" = CP437. Testa även Latin-1 och UTF-8. Använd chardet som fallback. Om svenska tecken (åäöÅÄÖ) inte ser korrekta ut efter dekodning, prova nästa kodning.
- Belopp: Parsa som Decimal först, konvertera sedan till int (ören) genom att multiplicera med 100 och runda. Validera att inget belopp tappar mer än 0.004 kr i konverteringen.
- Citattecken: Värden kan vara omslutna av citattecken ("Övningsbolaget AB") eller utan (Kaffebröd). Parsern måste hantera båda.
- Objektlistor i TRANS: Format `{dim1 obj1 dim2 obj2}` eller `{}` för tom. Parsa som lista av tupler.

**Detaljerad SIE4-syntax att hantera (baserat på exempelfilen):**

Metadata-rader (en per fil):
```
#FLAGGA 1
#FORMAT PC8
#SIETYP 4
#PROGRAM "programnamn" version
#GEN datum signatur
#FNAMN "företagsnamn"
#FNR "filnummer"
#ORGNR nnnnnn-nnnn
#ADRESS "kontakt" "gatuadress" "postadress" "telefon"
#RAR årindex startdatum slutdatum
#TAXAR år
#VALUTA valutakod
#KPTYP kontoplanstyp
```

Kontoplan (en rad per konto):
```
#KONTO kontonr kontonamn
#KTYP kontonr kontotyp        (T=tillgång, S=skuld, K=kostnad, I=intäkt)
#SRU kontonr srukod
```

Dimensioner och objekt:
```
#DIM dimensionsnr dimensionsnamn
#OBJEKT dimensionsnr objektid objektnamn
```

Saldon:
```
#IB årindex kontonr belopp
#UB årindex kontonr belopp
#RES årindex kontonr belopp
```
Årindex 0 = innevarande år, -1 = föregående år.

Verifikationer:
```
#VER serie nummer datum text [regdatum]
{
   #TRANS kontonr {objektlista} belopp [transtext] [kvantitet] [transinfo]
   #TRANS kontonr {objektlista} belopp
}
```
Objektlista kan vara `{}` (tom) eller `{1 Nord}` eller `{1 Nord 6 0001}` (flera dimensioner).

**Parsern ska:**
1. Läsa hela filen med korrekt teckenkodning
2. Parsa alla metadata-fält till Company-objektet
3. Bygga kontoplanen som dict[str, Account]
4. Parsa dimensioner och objekt
5. Parsa alla IB, UB, RES till respektive dict
6. Parsa alla verifikationer med sina transaktioner
7. Beräkna checksumma (SHA256) av hela källfilen

**Validering efter parsning (i parsern, inte separat):**
- Räkna antal #VER-rader i källfilen och jämför med antal parsade verifikationer. Måste matcha exakt.
- Räkna antal #TRANS-rader i källfilen och jämför med totalt antal parsade transaktioner. Måste matcha exakt.
- Verifiera att varje verifikation balanserar (summa av transaktioner = 0, med tolerans ±1 öre för avrundning).
- Verifiera att antal konton i #KONTO matchar antal konton i kontoplanen.
- Logga varje valideringsresultat. Om någon validering misslyckas: kasta ett tydligt undantag med detaljer, parsningen får INTE fortsätta med felaktig data.

**Publik funktion:**
```python
def parse_sie4(filepath: str) -> Company:
    """Parsar en SIE4-fil och returnerar ett Company-objekt.
    
    Raises:
        SIEParseError: Om filen inte kan parsas eller data inte validerar.
    """
```

### Test
Skapa `tests/test_sie_parser.py`:
- Testa mot `test_data/SIE4_Exempelfil.SE`
- Verifiera: företagsnamn = "Övningsbolaget AB", orgnr = "555555-5555"
- Verifiera: exakt 295 verifikationer
- Verifiera: totalt antal transaktioner stämmer (räkna #TRANS i filen)
- Verifiera: kontoplan innehåller förväntade konton (stickprov: 1910, 2641, 3041)
- Verifiera: IB/UB för konto 1930 stämmer (IB=938311.64, UB=746686.19 → i ören: 93831164, 74668619)
- Verifiera: dimensioner parsade (dimension 1 = "Resultatenhet", dimension 6 = "Projekt")
- Verifiera: objektlista i TRANS parsad korrekt (hitta en transaktion med {1 Nord 6 0001})
- Verifiera: verifikation A 1, datum 2021-01-05, text "Kaffebröd", 3 transaktioner som summerar till 0
- Verifiera: checksumma genererad och icke-tom

### Checkpoint 2
Kör: `pytest tests/test_sie_parser.py -v`
Alla tester ska vara gröna. Visa fullständig output och vänta på bekräftelse.

---

## STEG 3: CSV-importmodul

### Vad ska byggas
Skapa `src/core/csv_importer.py` — importmodul för bankutdrag.

**Kritiska krav:**
- Samma nolltolerans som SIE4-parsern. Inga rader får tyst försvinna.
- Belopp: Hantera svenskt format med decimalkomma och mellanrum som tusentalsavgränsare. "31 036,00" → 3103600 (ören). Uttag har minustecken: "-87 768,00" → -8776800.
- En kolumn kan vara tom (insättning tom vid uttag och vice versa). Tomt fält = 0.
- BOM: Filen har UTF-8 BOM (\xEF\xBB\xBF). Hantera detta.
- Radnummer: Varje BankTransaction ska ha source_row satt till radnummer i CSV (1-indexerat, header = rad 1).

**Formatet (SEB, baserat på exempelfilen):**
```
Bokförd;Valutadatum;Text;Typ;Insättningar;Uttag;Bokfört saldo
2025-12-30;2025-12-30;BG 5801-7161 ;Bankgirobetalning;31 036,00;;611 996,05
2025-12-30;2025-12-30;LBE5801-7161;Annan;;-87 768,00;580 960,05
```

- Separator: semikolon
- Datumformat: YYYY-MM-DD
- Belopp: svenskt format med decimalkomma, mellanrum som tusentalsavgränsare
- Insättningar i egen kolumn (positivt belopp), uttag i egen kolumn (negativt belopp)
- En av insättning/uttag är alltid tom per rad

**Parsern ska:**
1. Läsa CSV med korrekt encoding (utf-8-sig för BOM)
2. Validera att alla förväntade kolumner finns
3. Parsa varje rad till BankTransaction
4. Belopp: ta insättning om den finns, annars uttag. Konvertera till ören.
5. Validera löpande saldo: kontrollera att bokfört saldo stämmer rad för rad (saldo_föregående + belopp = saldo_nuvarande). OBS: raderna i filen är i omvänd kronologisk ordning (nyast först). Ta hänsyn till detta. Logga varje avvikelse men fortsätt parsningen (bankens avrundning kan ge ±1 öre).
6. Beräkna checksumma (SHA256) av källfilen

**Publik funktion:**
```python
def import_bank_csv(filepath: str, bank_format: str = "seb", config: dict = None) -> list[BankTransaction]:
    """Importerar bankutdrag från CSV.
    
    Args:
        filepath: Sökväg till CSV-fil
        bank_format: Bankformat att använda (default: "seb")
        config: Konfiguration med kolumnmappning etc. Om None, använd default.
    
    Raises:
        CSVImportError: Om filen inte kan parsas eller saknar förväntade kolumner.
    """
```

### Test
Skapa `tests/test_csv_importer.py`:
- Testa mot `test_data/Sophone_2025_transar.csv`
- Verifiera: exakt 263 transaktioner
- Verifiera: datumintervall 2025-01-03 till 2025-12-30
- Verifiera: första raden (rad 2 i CSV): datum 2025-12-30, belopp 3103600 (ören), typ "Bankgirobetalning", saldo 61199605
- Verifiera: en lönepost hittas (text "LÖNER", typ "Lön")
- Verifiera: uttag korrekt negativt (rad 3: -8776800 ören)
- Verifiera: att source_row stämmer på stickprov
- Verifiera: checksumma genererad

### Checkpoint 3
Kör: `pytest tests/test_csv_importer.py -v`
Alla tester ska vara gröna. Visa fullständig output och vänta på bekräftelse.

---

## STEG 4: Datakvalitetsvalidering

### Vad ska byggas
Skapa `src/core/data_validator.py` — övergripande datakvalitetsrapport.

**Denna modul körs EFTER parsning och producerar en rapport som bekräftar att all data är korrekt inläst.**

```python
@dataclass
class ValidationReport:
    file_path: str
    file_type: str  # "SIE4" eller "CSV"
    checksum: str
    timestamp: datetime
    total_records: int
    parsed_records: int
    warnings: list[str]
    errors: list[str]
    is_valid: bool  # True om inga errors (warnings ok)
    details: dict   # Typspecifika detaljer

def validate_sie4(company: Company, source_path: str) -> ValidationReport:
    """Validerar parsad SIE4-data mot källfil."""

def validate_bank_csv(transactions: list[BankTransaction], source_path: str) -> ValidationReport:
    """Validerar parsade banktransaktioner mot källfil."""

def print_validation_summary(reports: list[ValidationReport]) -> None:
    """Skriver ut en tydlig sammanfattning av alla valideringsrapporter."""
```

**SIE4-validering:**
- Antal verifikationer i parsad data vs antal #VER i källfil
- Antal transaktioner i parsad data vs antal #TRANS i källfil
- Antal konton i kontoplan vs antal #KONTO i källfil
- Alla verifikationer balanserar (debet = kredit ±1 öre)
- IB + periodens transaktioner = UB (per konto, om möjligt)
- Inga tomma verifikationstexter (varning, inte fel)
- Inga duplicerade verifikationsnummer inom samma serie

**CSV-validering:**
- Antal rader i parsad data vs antal datarader i källfil
- Saldoberäkning: verifiera löpande saldo rad för rad
- Inga tomma datumfält
- Inga belopp som är exakt 0 i både insättning och uttag (varning)
- Datumordning verifierad (ska vara fallande om SEB-format)

### Test
Skapa `tests/test_data_validator.py`:
- Kör validate_sie4 mot den parsade exempelfilen och verifiera is_valid = True
- Kör validate_bank_csv mot den parsade CSV:en och verifiera is_valid = True
- Testa med avsiktligt korrupt data (ta bort en verifikation) och verifiera is_valid = False

### Checkpoint 4
Kör: `pytest tests/test_data_validator.py -v`
Kör även: `python -c "from src.core.sie_parser import parse_sie4; from src.core.csv_importer import import_bank_csv; from src.core.data_validator import *; r1 = validate_sie4(parse_sie4('test_data/SIE4_Exempelfil.SE'), 'test_data/SIE4_Exempelfil.SE'); r2 = validate_bank_csv(import_bank_csv('test_data/Sophone_2025_transar.csv'), 'test_data/Sophone_2025_transar.csv'); print_validation_summary([r1, r2])"`
Visa valideringsrapporten och vänta på bekräftelse.

---

## STEG 5: Modul-basarkitektur och Modul A (Bank vs Huvudbok)

### Vad ska byggas

**5a. Abstrakt basklass:**
Skapa `src/modules/base_module.py`:
```python
from abc import ABC, abstractmethod

class AnalysisModule(ABC):
    """Basklass för alla analysmoduler."""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Modulens namn (t.ex. 'bank_matching')"""
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Kort beskrivning av vad modulen gör"""
    
    @abstractmethod
    def analyze(self, companies: list[Company], bank_transactions: dict[str, list[BankTransaction]], config: dict) -> list[Finding]:
        """Kör analysen och returnera fynd.
        
        Args:
            companies: Lista av Company-objekt
            bank_transactions: Dict med bolagsnamn/orgnr → banktransaktioner
            config: Konfiguration
        
        Returns:
            Lista av Finding-objekt
        """
    
    def _create_finding(self, category: str, risk_level: str, summary: str, companies: list[str], details: dict) -> Finding:
        """Hjälpmetod för att skapa Finding med auto-genererat ID."""
```

**5b. Modul A: Bank vs Huvudbok:**
Skapa `src/modules/bank_matching.py`:

Matchningslogik:
1. Hämta alla transaktioner på bankkonton (1910-1940) från SIE4-data
2. Hämta alla banktransaktioner från CSV
3. Matcha med algoritm:
   - Steg 1: Exakt beloppsmatch (inom ±tolerance) + datummatch (inom ±3 dagar) → stark match
   - Steg 2: Enbart beloppsmatch (inom ±tolerance) → svag match, flagga datumavvikelse
   - Steg 3: Omatchade poster åt båda håll → flagga som omatchade
4. Hantera 1-till-många: en bankpost kan matcha mot flera bokföringsposter som summerar till samma belopp (splitbetalningar)
5. Flagga dubbelträffar: om samma bokföringspost matchas mot flera bankposter

**Viktigt om beloppstoleransen:**
Konfigureras via `config.matching.amount_tolerance_bank_kr`. Standardvärde 50 kr. Toleransen konverteras till ören internt (50 kr = 5000 ören). Matchning: `abs(belopp_bok - belopp_bank) <= tolerance_ören`.

**Fynd som genereras:**

| Kategori | Risknivå | Villkor |
|----------|----------|---------|
| `unmatched_bank` | HIGH | Banktransaktion utan match i bokföringen |
| `unmatched_book` | HIGH | Bokföringstransaktion på bankkonto utan match i banken |
| `date_discrepancy` | MEDIUM | Match på belopp men >5 dagars datumskillnad |
| `amount_discrepancy` | MEDIUM | Nästan-match: belopp inom 1-50 kr skillnad |
| `double_match` | HIGH | En transaktion matchad mot flera motparter |

**Varje Finding.details ska innehålla:**
- Berörda transaktioner (med alla fält)
- Differens i belopp och datum
- Matchningsmetod som användes

### Test
Skapa `tests/test_modules/test_bank_matching.py`:
- Skapa syntetisk testdata: 10 vouchers med banktransaktioner, 10 matchande banktransaktioner, 2 extra i vardera som inte matchar
- Testa: alla starka matchningar hittas
- Testa: omatchade poster åt båda håll flaggas som HIGH
- Testa: datumavvikelse >5 dagar flaggas som MEDIUM
- Testa: beloppstolerans respekteras (49 kr diff = match, 51 kr diff = flagga)
- Kör även mot riktiga testfilerna (SIE4 + CSV) — eftersom de inte matchar ska ALLA vara omatchade. Verifiera att antalet fynd är rimligt.

### Checkpoint 5
Kör: `pytest tests/test_modules/test_bank_matching.py -v`
Kör även en integration mot testfilerna och visa antal fynd per kategori. Vänta på bekräftelse.

---

## STEG 6: Modul B (Koncernintern avstämning)

### Vad ska byggas
Skapa `src/modules/intercompany.py`:

**Analyslogik:**
1. Identifiera koncerninterna konton per bolag (kontointervall från config: 1660-1669, 2460-2469)
2. Hämta UB (utgående balans) på dessa konton per bolag
3. Bygg matris: Bolag A fordran → Bolag B skuld. Identifiera motparten via kontonamn (ofta "Fordran Bolag X") eller via manuell mappning i config
4. Beräkna differens per bolagspar med tolerans (±500 kr standard)
5. Analysera transaktionsmönster på koncerninterna konton: identifiera cirkelflöden (A→B→C→A)
6. Analysera saldomönster: växer fordran stadigt utan avräkning?

**Fynd:**

| Kategori | Risknivå | Villkor |
|----------|----------|---------|
| `ic_imbalance` | HIGH | Fordran hos A matchar inte skuld hos B (utöver tolerans) |
| `ic_one_sided` | HIGH | Koncernintern post utan identifierbar motpart |
| `ic_circular` | HIGH | Transaktionskedja som bildar cirkel genom ≥3 bolag |
| `ic_growing_balance` | MEDIUM | Saldo ökar >3 månader i rad utan avräkning |
| `ic_unexpected_party` | MEDIUM | Transaktion med bolag utanför känd sfär |

**OBS:** Eftersom exempelfilen bara innehåller ETT bolag kan vi inte fullt testa koncernavstämning. Skapa syntetisk testdata med 3 bolag (A, B, C) som har koncerninterna poster, varav några inte balanserar.

### Test
Skapa `tests/test_modules/test_intercompany.py`:
- Skapa 3 syntetiska Company-objekt med koncerninterna konton
- Testa: balanserad fordran/skuld ger inga fynd
- Testa: obalans utöver tolerans flaggas som HIGH
- Testa: ensidig post (fordran utan motsvarande skuld) flaggas
- Testa: tolerans respekteras (499 kr diff = ok, 501 kr diff = flagga)

### Checkpoint 6
Kör: `pytest tests/test_modules/test_intercompany.py -v`
Visa testresultat och vänta på bekräftelse.

---

## STEG 7: Modul C (Avräkningskonton) och Modul D (Beloppsmönster)

### Modul C: Avräkningskonton
Skapa `src/modules/clearing_accounts.py`:

Analysera konton definierade i config (1630-serien, 2393, 2890-serien):
- Öppna saldon >90 dagar utan rörelse
- Hög transaktionsfrekvens jämfört med omsättning
- Saldo som växer stadigt
- Transaktioner mot okända motparter (kontrollera verifikationstext)
- Specifikt: ägarlån (2393) — alla transaktioner flaggas med MEDIUM, stora belopp med HIGH

### Modul D: Beloppsmönster och Benfords lag
Skapa `src/modules/amount_patterns.py`:

**Benfords lag:**
- Extrahera första siffran från alla transaktionsbelopp (absolutbelopp >10 kr, för att undvika brus)
- Beräkna faktisk fördelning vs teoretisk Benford-fördelning
- Använd chi-kvadrat-test (scipy.stats.chisquare) med konfigurerbar signifikansnivå
- Kör per bolag och för hela sfären

**Upprepade belopp:**
- Räkna frekvens av varje unikt belopp
- Flagga belopp som förekommer >5 gånger (tröskelvärde konfigurerbart) med samma motpart/kontotext

**Strax under gränsvärden:**
- Identifiera kluster av belopp strax under konfigurerade trösklar
- Default-trösklar: 10 000, 20 000, 25 000, 50 000, 100 000 kr
- "Strax under" = inom 5% under tröskeln (t.ex. 23 750–24 999 för 25 000-gränsen)
- Flagga om >3 transaktioner i samma intervall

**Jämna belopp:**
- Beräkna andel transaktioner med belopp som är jämnt delbart med 1000 kr
- Om >25% av transaktionerna på ett konto är jämna → flagga

### Test
Tester för båda modulerna med syntetisk data.

### Checkpoint 7
Kör: `pytest tests/test_modules/test_clearing_accounts.py tests/test_modules/test_amount_patterns.py -v`
Visa testresultat och vänta på bekräftelse.

---

## STEG 8: Modul E (Tidsmönster) och Modul F (Löner/kreditkort)

### Modul E: Tidsmönster
Skapa `src/modules/time_patterns.py`:

- Bokslutskluster: räkna transaktioner per dag, flagga om sista 5 dagarna före bokslutsperiod har >2x normalt dagligt genomsnitt
- Reverseringar: hitta transaktioner nära bokslut som har en exakt omvänd transaktion inom 30 dagar efter bokslut
- Helgtransaktioner: flagga verifikationer daterade på lördag/söndag eller svenska helgdagar (nyårsdagen, trettondedag jul, långfredag, påskdagen, annandag påsk, 1 maj, kristi himmelfärd, nationaldagen, midsommarafton, julafton, juldagen, annandag jul, nyårsafton)
- Periodfixering: identifiera systematisk förskjutning av kostnader (konto 4xxx-7xxx) till sent i perioden
- Luckor: perioder >14 dagar utan bokföring (exklusive juli) följda av intensiv aktivitet

### Modul F: Löner och kreditkort
Skapa `src/modules/salary_expenses.py`:

- Avvikande löner: beräkna medel och stddev per lönekonto per månad, flagga poster >2 stddev
- Dubbelbetalning: samma belopp på lönekonto >1 gång samma månad
- Kreditkortsfakturor: identifiera poster på konto 6540-6570 eller med text "Visa"/"Mastercard"/"kreditkort", flagga belopp >20 000 kr
- Okända uttag: transaktioner på bankkonto utan matchande verifikation med tydlig text

### Test och Checkpoint 8
Samma mönster — syntetisk testdata, pytest, vänta på bekräftelse.

---

## STEG 9: Modul G (Verksamhetsrelevans)

### Vad ska byggas
Skapa `src/modules/business_relevance.py`:

**Denna modul kräver tillgång till internet (allabolag.se) och Claude API.**

Analyslogik:
1. Extrahera unika motpartsnamn från verifikationstexter och banktransaktionstexter
2. Rensa och normalisera namn (ta bort "AB", "HB", trimma, etc.)
3. Gör uppslag via web scraping eller API mot allabolag.se:
   - Sök på företagsnamn
   - Hämta: SNI-kod, verksamhetsbeskrivning, status (aktivt/avregistrerat)
   - Cacha resultat lokalt för att undvika upprepade anrop
4. Skicka transaktionstext + motpartens verksamhetsbeskrivning till Claude API och be om bedömning: "Är denna transaktion förenlig med en IT-verksamhet som säljer produkter och tjänster till företag?"
5. Claude svarar med: ja/nej/osäkert + kort motivering

**Fynd:**

| Kategori | Risknivå | Villkor |
|----------|----------|---------|
| `unknown_counterparty` | MEDIUM | Motpart kan inte identifieras via allabolag.se |
| `unrelated_business` | MEDIUM | Motpartens verksamhet har ingen tydlig IT-koppling |
| `private_supplier` | HIGH | Betalning till privatperson/enskild firma |
| `luxury_consumption` | MEDIUM | Transaktion som tyder på representation/lyx utanför kärnverksamhet |
| `geographic_anomaly` | MEDIUM | Motpart i oväntat land/region |

**Fallback:** Om allabolag.se inte är nåbart, logga varning och fall tillbaka på enbart AI-bedömning av transaktionstexten.

### Test
- Testa med mockad allabolag.se-data (3 företag: ett IT-företag, en blomsterhandel, ett okänt)
- Testa att cacha fungerar (andra anropet ska inte göra HTTP-request)
- Testa Claude API-anropet med mockad respons

### Checkpoint 9
Kör tester och visa resultat. Vänta på bekräftelse.

---

## STEG 10: Output — Resultatfiler och HTML-dashboard

### Vad ska byggas

**10a. Resultatgenerator** (`src/output/report_generator.py`):

```python
def generate_results_json(findings: list[Finding], companies: list[Company], 
                          validation_reports: list[ValidationReport]) -> dict:
    """Genererar komplett resultat-JSON med alla fynd och metadata."""

def generate_context_json(findings: list[Finding], companies: list[Company]) -> dict:
    """Genererar kontextfil optimerad för att ladda in i Claude-konversation.
    Inkluderar: sammanfattning, alla fynd med status, öppna frågor, datareferenser."""

def save_results(output_dir: str, results: dict, context: dict) -> None:
    """Sparar resultat.json och kontext.json till output-mappen."""
```

Kontextfilen ska vara strukturerad så att en människa kan ladda in den i en Claude-konversation och säga "Här är min analys, jag vill gräva vidare i fynd X". Den ska innehålla:
- Sfärsammanfattning (bolag, perioder, total debet/kredit)
- Alla fynd med risk_level, summary och ai_reasoning
- Öppna frågor som orkestreraren identifierat
- Instruktion till Claude om att den har rollen som forensisk revisorsassistent

**10b. HTML-dashboard** (`src/output/dashboard.py`):

Generera EN ENDA HTML-fil (ingen extern CSS/JS) som kan öppnas lokalt i webbläsaren.

Dashboard-vyer:
1. **Översikt**: totalt antal fynd per risknivå (CRITICAL/HIGH/MEDIUM/LOW/INFO), antal per modul, antal per bolag. Visa som sammanfattningskort med siffror.
2. **Fyndlista**: tabell med alla fynd, sorterbar och filtrerbar på risknivå/modul/bolag. Klickbar rad som expanderar och visar AI-resonemang + underliggande transaktioner.
3. **Bolagsvy**: välj bolag i dropdown, se alla fynd för det bolaget.
4. **Datakvalitet**: visa valideringsrapporterna.

Använd inline CSS. Färgkodning: röd = CRITICAL/HIGH, orange = MEDIUM, grå = LOW/INFO.
Filtrering och sortering med vanilla JavaScript (ingen React/Vue behövs).
Dashboarden läser in results.json som embeddad JSON i HTML-filen (en <script>-tagg med `const DATA = {...}`).

### Test
- Generera resultat från testfilerna (alla moduler mot SIE4 + CSV)
- Verifiera att JSON-filerna är giltiga och innehåller alla förväntade fält
- Verifiera att HTML-filen skapas och innehåller korrekt JSON
- Öppna HTML-filen manuellt och kontrollera att den renderar korrekt (manuellt steg)

### Checkpoint 10
Kör generering mot testdata, visa att filerna skapas korrekt, lista antal fynd per modul. Vänta på bekräftelse.

---

## STEG 11: AI-orkestrerare

### Vad ska byggas
Skapa `src/orchestrator/engine.py` och `src/orchestrator/prompts.py`.

**engine.py — OrchestrationEngine:**

```python
class OrchestrationEngine:
    def __init__(self, config: dict, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.config = config
        self.findings: list[Finding] = []
        self.execution_log: list[dict] = []
    
    def run_full_analysis(self, companies: list[Company], 
                          bank_transactions: dict[str, list[BankTransaction]]) -> AnalysisResult:
        """Kör fullständig analys i 5 steg."""
    
    def enrich_findings_with_ai(self, findings: list[Finding], companies: list[Company]) -> list[Finding]:
        """Skicka varje fynd till Claude för resonemang och riskbedömning."""
    
    def cross_reference_findings(self, findings: list[Finding]) -> list[Finding]:
        """Identifiera mönster som spänner över flera moduler/bolag."""
    
    def generate_summary(self, findings: list[Finding], companies: list[Company]) -> str:
        """Generera övergripande sammanfattning med prioriterade åtgärder."""
```

**Orkestreringsflöde:**
1. Kör datakvalitetsvalidering. Om någon fil inte validerar → stopp.
2. Kör Modul A (bank vs bok) och Modul B (koncernintern) för alla bolag.
3. Skicka resultaten till Claude och fråga: "Baserat på dessa fynd, vilka ytterligare moduler bör köras och för vilka bolag?" Claude svarar med JSON-lista av moduler att köra.
4. Kör de moduler Claude föreslår.
5. Skicka ALLA fynd till Claude för korskoppling och sammanfattning.
6. Berika varje fynd med AI-resonemang (batcha i grupper om max 20 fynd per API-anrop).
7. Generera output-filer.

**prompts.py:**

Definiera promptmallar som Python f-strings eller template strings:
- `SYSTEM_PROMPT`: Systemroll för Claude som forensisk revisorsassistent
- `ENRICHMENT_PROMPT`: Prompt för att resonera kring enskilda fynd
- `CROSS_REFERENCE_PROMPT`: Prompt för att hitta mönster mellan fynd
- `ORCHESTRATION_PROMPT`: Prompt för att bestämma nästa steg
- `SUMMARY_PROMPT`: Prompt för övergripande sammanfattning

Alla prompter ska instruera Claude att svara i strukturerad JSON.

**Felhantering för API-anrop:**
- Retry med exponential backoff (max 3 retries)
- Om API inte nåbar: logga varning, fortsätt utan AI-resonemang (fynden finns ändå)
- Rate limiting: max 10 anrop per minut (konfigurerbart)

### Test
- Testa med mockad Claude API (returnera förutsägbara JSON-svar)
- Verifiera att orkestreringsflödet följer alla 7 steg
- Verifiera att alla fynd berikas med ai_reasoning
- Verifiera retry-logik

### Checkpoint 11
Kör tester. Visa orkestreringsloggen. Vänta på bekräftelse.

---

## STEG 12: Main-skript och integration

### Vad ska byggas
Skapa `main.py`:

```python
"""
Forensisk Bokföringsanalys — Huvudskript
Användning: python main.py --config config/wooz_config.yaml --sie-dir ./data/sie/ --csv-dir ./data/csv/ --output ./output/
"""
import argparse

def main():
    parser = argparse.ArgumentParser(description="Forensisk bokföringsanalys")
    parser.add_argument("--config", required=True, help="Sökväg till konfigurationsfil")
    parser.add_argument("--sie-dir", required=True, help="Mapp med SIE4-filer")
    parser.add_argument("--csv-dir", required=True, help="Mapp med bankutdrag (CSV)")
    parser.add_argument("--output", default="./output", help="Output-mapp")
    parser.add_argument("--api-key", help="Claude API-nyckel (eller sätt ANTHROPIC_API_KEY)")
    parser.add_argument("--no-ai", action="store_true", help="Kör utan AI-resonemang")
    parser.add_argument("--modules", nargs="+", help="Specifika moduler att köra (default: alla)")
    args = parser.parse_args()
    
    # 1. Ladda config
    # 2. Parsa alla SIE4-filer i sie-dir
    # 3. Importera alla CSV-filer i csv-dir
    # 4. Validera all data, visa rapport
    # 5. Om --no-ai: kör alla moduler utan orkestrerare
    #    Annars: kör via OrchestrationEngine
    # 6. Generera output (results.json, context.json, dashboard.html)
    # 7. Skriv ut sammanfattning till terminal
```

Skapa även `README.md` med:
- Projektbeskrivning
- Installation (pip install -r requirements.txt)
- Användning (exempelkommandon)
- Konfiguration (förklara YAML-filerna)
- Beskrivning av modulerna
- Output-format

### Test
- Kör `python main.py --config config/default_config.yaml --sie-dir test_data/ --csv-dir test_data/ --output test_output/ --no-ai`
- Verifiera att output-mappen skapas med results.json, context.json och dashboard.html
- Verifiera att terminalen visar sammanfattning

### Checkpoint 12 (FINAL)
Kör fullständig integration. Visa:
1. Valideringsrapport
2. Antal fynd per modul och risknivå
3. Att alla output-filer skapats
4. Att dashboard.html öppnas korrekt

Vänta på slutgiltig bekräftelse.

---

## Sammanfattning av checkpoints

| Steg | Checkpoint | Vad verifieras |
|------|-----------|----------------|
| 1 | Modeller + config | Import fungerar, YAML laddar |
| 2 | SIE4-parser | 295 verifikationer, korrekt data, alla tester gröna |
| 3 | CSV-import | 263 transaktioner, korrekt belopp/datum, alla tester gröna |
| 4 | Datavalidering | Valideringsrapport visar grön status för båda testfiler |
| 5 | Modul A | Bankmatchning fungerar, tolerans respekteras |
| 6 | Modul B | Koncernavstämning fungerar med syntetisk data |
| 7 | Modul C+D | Avräkningskonton + Benford fungerar |
| 8 | Modul E+F | Tidsmönster + löneanalys fungerar |
| 9 | Modul G | Verksamhetsrelevans med mockad extern data |
| 10 | Output | JSON + HTML genereras korrekt |
| 11 | Orkestrerare | AI-flöde fungerar med mockad API |
| 12 | Integration | Fullständig körning ände till ände |
