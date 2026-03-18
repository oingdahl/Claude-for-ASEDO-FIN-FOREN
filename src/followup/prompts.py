"""Promptmallar för interaktivt uppföljningsläge."""

from __future__ import annotations

INVESTIGATOR_SYSTEM_PROMPT = """Du är en erfaren forensisk revisor som granskar den svenska bolagssfären {sphere_name}.

Sfärens verksamhet: {business_description}

Bolag i sfären:
{company_list}

Du har redan genomfört en fullständig analys och hittat {total_findings} fynd, varav {critical_count} kritiska och {high_count} med hög risk.

Du har tillgång till följande verktyg för att hämta data:
{tool_descriptions}

Revisorns tidigare frågor i denna session:
{conversation_summary}

VIKTIGA PRINCIPER:
- Svara ALLTID på svenska
- Var konkret: referera till specifika transaktioner, belopp, datum och bolag
- Om du behöver mer data, använd verktygen — gissa inte
- Om du hittar nya misstänkta mönster, beskriv dem tydligt
- Ge alltid en rekommendation: vad bör revisorn göra härnäst?
- Om en fråga kräver att en modul körs om med nya parametrar, föreslå det explicit"""

INVESTIGATION_TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_transactions",
        "description": "Hämta transaktioner filtrerat på bolag, konto, belopp, datum eller motpart",
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {
                    "type": "string",
                    "description": "Bolagsnamn eller org_nr (valfritt, tomt = alla bolag)",
                },
                "account": {"type": "string", "description": "Kontonummer (valfritt)"},
                "amount_min_kr": {"type": "number", "description": "Minbelopp i kr (valfritt)"},
                "amount_max_kr": {"type": "number", "description": "Maxbelopp i kr (valfritt)"},
                "start_date": {
                    "type": "string",
                    "description": "Startdatum YYYY-MM-DD (valfritt)",
                },
                "end_date": {"type": "string", "description": "Slutdatum YYYY-MM-DD (valfritt)"},
                "text_search": {
                    "type": "string",
                    "description": "Sök i verifikationstext (valfritt)",
                },
                "counterparty": {
                    "type": "string",
                    "description": "Motpart att söka efter (valfritt)",
                },
            },
        },
    },
    {
        "name": "get_monthly_totals",
        "description": "Hämta månatliga summor för ett konto i ett bolag — perfekt för trendanalys",
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Bolagsnamn eller org_nr"},
                "account": {"type": "string", "description": "Kontonummer"},
            },
            "required": ["company", "account"],
        },
    },
    {
        "name": "trace_money",
        "description": "Spåra ett belopp genom sfären — hitta matchande transaktioner mellan bolag",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount_kr": {"type": "number", "description": "Belopp att spåra i kr"},
                "tolerance_kr": {
                    "type": "number",
                    "description": "Tolerans i kr (default 500)",
                },
                "start_date": {
                    "type": "string",
                    "description": "Startdatum YYYY-MM-DD (valfritt)",
                },
                "end_date": {"type": "string", "description": "Slutdatum YYYY-MM-DD (valfritt)"},
            },
            "required": ["amount_kr"],
        },
    },
    {
        "name": "compare_companies",
        "description": "Jämför ett konto eller ett mönster över alla bolag i sfären",
        "input_schema": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Kontonummer att jämföra"}
            },
            "required": ["account"],
        },
    },
    {
        "name": "get_findings",
        "description": "Hämta specifika fynd med detaljer",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lista av fynd-ID (valfritt)",
                },
                "company": {"type": "string", "description": "Filtrera på bolag (valfritt)"},
                "module": {"type": "string", "description": "Filtrera på modul (valfritt)"},
                "risk_level": {
                    "type": "string",
                    "description": "Filtrera på risknivå: CRITICAL/HIGH/MEDIUM/LOW/INFO (valfritt)",
                },
            },
        },
    },
    {
        "name": "get_intercompany_matrix",
        "description": "Visa hela matrisen av koncerninterna fordringar och skulder",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "rerun_module",
        "description": "Kör om en analysmodul med nya parametrar. Använd detta om du behöver en djupare analys av ett specifikt område.",
        "input_schema": {
            "type": "object",
            "properties": {
                "module": {
                    "type": "string",
                    "description": "Modulnamn: bank_matching, intercompany, clearing_accounts, amount_patterns, time_patterns, salary_expenses",
                },
                "company_filter": {
                    "type": "string",
                    "description": "Kör bara för detta bolag (valfritt)",
                },
                "start_date": {
                    "type": "string",
                    "description": "Begränsa till period fr.o.m. (valfritt)",
                },
                "end_date": {
                    "type": "string",
                    "description": "Begränsa till period t.o.m. (valfritt)",
                },
                "custom_params": {
                    "type": "object",
                    "description": "Extra parametrar specifika för modulen (valfritt)",
                },
            },
            "required": ["module"],
        },
    },
]
