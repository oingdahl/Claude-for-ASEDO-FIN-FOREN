"""Promptmallar för AI-orkestreraren.

Alla prompter instruerar Claude att svara i strukturerad JSON.
Används av OrchestrationEngine i engine.py.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Systemroll
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Du är en forensisk revisorsassistent med djup expertis inom:
- Svensk redovisningslagstiftning (ÅRL, BFL, K2/K3)
- Forensisk revision och utredningsmetodik
- Mönsteranalys i bokföringsdata
- SIE4-format och bankavstämning

Du analyserar bokföringsdata från en bolagssfär och identifierar avvikelser,
röda flaggor och misstänkta mönster. Svara alltid på svenska. Svara alltid i
den JSON-format som efterfrågas — inga förklaringar utanför JSON."""


# ---------------------------------------------------------------------------
# Orkesterering: vilka moduler ska köras?
# ---------------------------------------------------------------------------

ORCHESTRATION_PROMPT = """Baserat på följande initiala fynd från bank-bokföringsavstämning
och koncernintern analys, bedöm vilka ytterligare analysmoduler som bör köras.

Tillgängliga moduler:
- clearing_accounts: Avräkningskonton och ägarlån
- amount_patterns: Benfords lag, upprepade belopp, belopp under gränsvärden
- time_patterns: Helgtransaktioner, bokslutskluster, reverseringar, aktivitetsluckor
- salary_expenses: Avvikande löner, dubbelbetalningar, kreditkort
- business_relevance: Motpartsbedömning via extern databas + AI

Initiala fynd (urval, max 50):
{findings_summary}

Antal fynd per kategori: {category_counts}

Svara med JSON:
{{
  "modules": ["modul1", "modul2"],
  "reasoning": "Motivering till valda moduler",
  "priority_companies": ["org_nr1"]
}}"""


# ---------------------------------------------------------------------------
# Berikning av enskilda fynd
# ---------------------------------------------------------------------------

ENRICHMENT_PROMPT = """Analysera följande {count} forensiska fynd och ge resonemang
för varje fynd. Bedöm om fyndet sannolikt är en felaktighet, ett mönster av
intresse, eller ett bevis på oegentligheter.

Sfärkontext: {sphere_context}

Fynd:
{findings_json}

Svara med JSON-array — ett objekt per fynd, i samma ordning:
[
  {{
    "finding_id": "...",
    "reasoning": "Detaljerat resonemang kring fyndet (2-4 meningar)",
    "risk_assessment": "Förklaring av risknivån",
    "suggested_action": "Rekommenderad nästa åtgärd"
  }}
]"""


# ---------------------------------------------------------------------------
# Korskoppling av fynd
# ---------------------------------------------------------------------------

CROSS_REFERENCE_PROMPT = """Analysera samtliga {total} fynd nedan och identifiera
mönster som spänner över flera moduler eller bolag. Leta efter:
- Samma motparter som dyker upp i flera fynd
- Tidsmässiga samband (kluster av händelser)
- Belopp som återkommer i olika sammanhang
- Bolagsövergripande beteenden

Fynd (sammanfattning per kategori):
{category_summary}

Tidsperiod: {period}
Bolag i sfären: {companies}

Svara med JSON:
{{
  "patterns": [
    {{
      "pattern_id": "P001",
      "description": "Beskrivning av mönstret",
      "related_findings": ["fynd-id-1", "fynd-id-2"],
      "risk_level": "HIGH/MEDIUM/LOW",
      "reasoning": "Varför är detta ett mönster av intresse?"
    }}
  ],
  "connections": [
    {{
      "from_finding": "fynd-id",
      "to_finding": "fynd-id",
      "connection_type": "same_counterparty/temporal/amount_match"
    }}
  ]
}}"""


# ---------------------------------------------------------------------------
# Sammanfattning
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """Du har analyserat bokföringsdata för följande bolag: {companies}.
Perioden är {period}.

Totalt antal fynd: {total_findings}
Fördelning per risknivå: {risk_distribution}
Fördelning per modul: {module_distribution}

Toppfynd (HIGH/CRITICAL, max 10):
{top_findings}

Identifierade mönster:
{patterns}

Generera en forensisk revisorsrapport med:
1. Övergripande bedömning av sfärens ekonomiska integritet
2. De 3-5 viktigaste fynden att agera på omedelbart
3. Rekommenderade utredningsåtgärder
4. Eventuella frågetecken som kräver ytterligare information

Svara med JSON:
{{
  "executive_summary": "2-3 meningar övergripande bedömning",
  "risk_level_overall": "CRITICAL/HIGH/MEDIUM/LOW",
  "priority_actions": [
    {{
      "rank": 1,
      "action": "Konkret åtgärd",
      "related_findings": ["fynd-id"],
      "urgency": "OMEDELBAR/INOM_EN_VECKA/INOM_EN_MANAD"
    }}
  ],
  "open_questions": ["Fråga 1", "Fråga 2"],
  "summary": "Fullständig sammanfattning (5-10 meningar)"
}}"""
