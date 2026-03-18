"""Genererar ett fristående HTML-dashboard (inline CSS + vanilla JS).

Funktioner:
  generate_dashboard(results, output_path)  →  Skriver dashboard.html
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_RISK_COLORS = {
    "CRITICAL": "#c0392b",
    "HIGH":     "#e74c3c",
    "MEDIUM":   "#e67e22",
    "LOW":      "#7f8c8d",
    "INFO":     "#95a5a6",
}


def _risk_badge(level: str) -> str:
    color = _RISK_COLORS.get(level, "#95a5a6")
    return (
        f'<span style="background:{color};color:#fff;padding:2px 7px;'
        f'border-radius:3px;font-size:0.8em;font-weight:bold">{level}</span>'
    )


def _html(results: dict) -> str:
    data_json = json.dumps(results, ensure_ascii=False, default=str)
    summary = results.get("summary", {})
    by_risk = summary.get("by_risk_level", {})
    by_module = summary.get("by_module", {})
    by_company = summary.get("by_company", {})
    findings = results.get("findings", [])
    companies = results.get("companies", [])
    val_reports = results.get("validation_reports", [])
    generated_at = results.get("generated_at", "")

    # Sammanfattningskort (risknivå)
    risk_cards = ""
    for level in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]:
        count = by_risk.get(level, 0)
        color = _RISK_COLORS.get(level, "#95a5a6")
        risk_cards += (
            f'<div class="card" style="border-top:4px solid {color}">'
            f'<div class="card-num" style="color:{color}">{count}</div>'
            f'<div class="card-lbl">{level}</div></div>'
        )

    # Modulkort
    module_rows = "".join(
        f"<tr><td>{mod}</td><td><b>{cnt}</b></td></tr>"
        for mod, cnt in sorted(by_module.items(), key=lambda x: -x[1])
    )

    # Bolagskort
    company_rows = "".join(
        f"<tr><td>{org}</td><td><b>{cnt}</b></td></tr>"
        for org, cnt in sorted(by_company.items(), key=lambda x: -x[1])
    )

    # Fyndtabell
    finding_rows = ""
    for f in findings:
        risk = f.get("risk_level", "INFO")
        color = _RISK_COLORS.get(risk, "#95a5a6")
        det_json = json.dumps(f.get("details", {}), ensure_ascii=False, indent=2, default=str)
        ai = f.get("ai_reasoning") or ""
        companies_str = ", ".join(f.get("companies", []))
        finding_rows += f"""
        <tr class="finding-row" data-risk="{risk}" data-module="{f.get('module','')}"
            data-company="{companies_str}"
            onclick="toggleDetail(this)">
          <td><span class="dot" style="background:{color}"></span></td>
          <td>{f.get('finding_id','')}</td>
          <td>{_risk_badge(risk)}</td>
          <td>{f.get('module','')}</td>
          <td>{f.get('category','')}</td>
          <td style="max-width:400px">{f.get('summary','')}</td>
          <td>{companies_str}</td>
        </tr>
        <tr class="detail-row" style="display:none">
          <td colspan="7">
            <div class="detail-box">
              {"<p><b>AI-resonemang:</b> " + ai + "</p>" if ai else ""}
              <pre style="margin:0;font-size:0.8em;white-space:pre-wrap">{det_json}</pre>
            </div>
          </td>
        </tr>"""

    # Valideringsrapporter
    val_rows = ""
    for r in val_reports:
        ok = r.get("is_valid", False)
        status_col = '#27ae60' if ok else '#c0392b'
        status_txt = "OK" if ok else "FEL"
        warns = len(r.get("warnings", []))
        errs = len(r.get("errors", []))
        val_rows += (
            f'<tr><td>{Path(r.get("file_path","")).name}</td>'
            f'<td>{r.get("file_type","")}</td>'
            f'<td style="color:{status_col};font-weight:bold">{status_txt}</td>'
            f'<td>{r.get("parsed_records",0)} / {r.get("total_records",0)}</td>'
            f'<td>{warns}</td><td>{errs}</td></tr>'
        )

    # Unika moduler och bolag för filter-dropdowns
    unique_modules = sorted({f.get("module", "") for f in findings})
    unique_companies = sorted({org for f in findings for org in f.get("companies", [])})
    module_opts = "".join(f'<option value="{m}">{m}</option>' for m in unique_modules)
    company_opts = "".join(f'<option value="{c}">{c}</option>' for c in unique_companies)

    return f"""<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="UTF-8">
<title>Forensisk Bokföringsanalys — Dashboard</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #f4f6f9; color: #2c3e50; }}
  header {{ background: #2c3e50; color: #fff; padding: 16px 24px; }}
  header h1 {{ font-size: 1.4em; }}
  header small {{ opacity: 0.7; font-size: 0.8em; }}
  nav {{ display: flex; background: #34495e; }}
  nav button {{ flex: 1; padding: 12px; border: none; background: none; color: #ecf0f1;
                cursor: pointer; font-size: 0.95em; border-bottom: 3px solid transparent; }}
  nav button.active, nav button:hover {{ border-bottom-color: #3498db; color: #fff; }}
  .view {{ display: none; padding: 20px 24px; }}
  .view.active {{ display: block; }}
  .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
  .card {{ background: #fff; border-radius: 6px; padding: 16px 20px; min-width: 120px;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  .card-num {{ font-size: 2em; font-weight: 700; }}
  .card-lbl {{ font-size: 0.85em; color: #7f8c8d; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           border-radius: 6px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  th {{ background: #2c3e50; color: #fff; padding: 10px 12px; text-align: left;
        font-size: 0.85em; cursor: pointer; user-select: none; }}
  th:hover {{ background: #34495e; }}
  td {{ padding: 9px 12px; font-size: 0.875em; border-bottom: 1px solid #ecf0f1; }}
  tr.finding-row {{ cursor: pointer; }}
  tr.finding-row:hover td {{ background: #f8f9fa; }}
  .detail-box {{ background: #f8f9fa; padding: 14px; border-left: 4px solid #3498db;
                 margin: 4px 0; border-radius: 0 4px 4px 0; }}
  .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; }}
  .filter-bar {{ display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }}
  .filter-bar input, .filter-bar select {{
    padding: 7px 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 0.875em; }}
  .filter-bar input {{ flex: 1; min-width: 200px; }}
  h2 {{ font-size: 1.1em; margin-bottom: 14px; color: #2c3e50; }}
  h3 {{ font-size: 0.95em; margin: 16px 0 8px; color: #34495e; }}
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  @media(max-width:700px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<header>
  <h1>Forensisk Bokföringsanalys</h1>
  <small>Genererad: {generated_at} &nbsp;|&nbsp; Totalt {len(findings)} fynd</small>
</header>
<nav>
  <button class="active" onclick="showView('overview',this)">Översikt</button>
  <button onclick="showView('findings',this)">Fynd ({len(findings)})</button>
  <button onclick="showView('companies',this)">Bolag</button>
  <button onclick="showView('quality',this)">Datakvalitet</button>
</nav>

<!-- ÖVERSIKT -->
<div id="view-overview" class="view active">
  <h2>Fynd per risknivå</h2>
  <div class="cards">{risk_cards}</div>
  <div class="two-col">
    <div>
      <h3>Per modul</h3>
      <table>
        <tr><th>Modul</th><th>Antal</th></tr>
        {module_rows}
      </table>
    </div>
    <div>
      <h3>Per bolag</h3>
      <table>
        <tr><th>Org.nr</th><th>Antal</th></tr>
        {company_rows}
      </table>
    </div>
  </div>
</div>

<!-- FYNDLISTA -->
<div id="view-findings" class="view">
  <div class="filter-bar">
    <input id="search" type="text" placeholder="Sök i sammanfattning..." oninput="filterFindings()">
    <select id="filter-risk" onchange="filterFindings()">
      <option value="">Alla risknivåer</option>
      <option value="CRITICAL">CRITICAL</option>
      <option value="HIGH">HIGH</option>
      <option value="MEDIUM">MEDIUM</option>
      <option value="LOW">LOW</option>
      <option value="INFO">INFO</option>
    </select>
    <select id="filter-module" onchange="filterFindings()">
      <option value="">Alla moduler</option>
      {module_opts}
    </select>
    <select id="filter-company" onchange="filterFindings()">
      <option value="">Alla bolag</option>
      {company_opts}
    </select>
  </div>
  <table id="findings-table">
    <thead><tr>
      <th></th>
      <th onclick="sortTable(1)">ID ▾</th>
      <th onclick="sortTable(2)">Risk</th>
      <th onclick="sortTable(3)">Modul</th>
      <th onclick="sortTable(4)">Kategori</th>
      <th>Sammanfattning</th>
      <th>Bolag</th>
    </tr></thead>
    <tbody id="findings-body">
      {finding_rows}
    </tbody>
  </table>
</div>

<!-- BOLAGSVY -->
<div id="view-companies" class="view">
  <div class="filter-bar">
    <select id="company-select" onchange="filterByCompany()">
      <option value="">Välj bolag...</option>
      {company_opts}
    </select>
  </div>
  <table id="company-findings-table">
    <thead><tr>
      <th></th><th>ID</th><th>Risk</th><th>Modul</th><th>Kategori</th><th>Sammanfattning</th>
    </tr></thead>
    <tbody id="company-findings-body"></tbody>
  </table>
</div>

<!-- DATAKVALITET -->
<div id="view-quality" class="view">
  <h2>Valideringsrapporter</h2>
  <table>
    <thead><tr>
      <th>Fil</th><th>Typ</th><th>Status</th><th>Poster (parsade/förväntade)</th>
      <th>Varningar</th><th>Fel</th>
    </tr></thead>
    <tbody>{val_rows}</tbody>
  </table>
</div>

<script>
const DATA = {data_json};

function showView(id, btn) {{
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('view-' + id).classList.add('active');
  btn.classList.add('active');
}}

function toggleDetail(row) {{
  const next = row.nextElementSibling;
  next.style.display = next.style.display === 'none' ? '' : 'none';
}}

function filterFindings() {{
  const search = document.getElementById('search').value.toLowerCase();
  const risk   = document.getElementById('filter-risk').value;
  const module = document.getElementById('filter-module').value;
  const company = document.getElementById('filter-company').value;
  let visible = 0;
  const rows = document.querySelectorAll('#findings-body .finding-row');
  rows.forEach(row => {{
    const detailRow = row.nextElementSibling;
    const riskOk    = !risk    || row.dataset.risk    === risk;
    const moduleOk  = !module  || row.dataset.module  === module;
    const companyOk = !company || row.dataset.company.includes(company);
    const textOk    = !search  || row.innerText.toLowerCase().includes(search);
    const show = riskOk && moduleOk && companyOk && textOk;
    row.style.display      = show ? '' : 'none';
    detailRow.style.display = 'none';
    if (show) visible++;
  }});
}}

function filterByCompany() {{
  const company = document.getElementById('company-select').value;
  const tbody = document.getElementById('company-findings-body');
  tbody.innerHTML = '';
  if (!company) return;
  DATA.findings.filter(f => f.companies.includes(company)).forEach(f => {{
    const colors = {{'CRITICAL':'#c0392b','HIGH':'#e74c3c','MEDIUM':'#e67e22','LOW':'#7f8c8d','INFO':'#95a5a6'}};
    const c = colors[f.risk_level] || '#95a5a6';
    tbody.innerHTML += `<tr>
      <td><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${{c}}"></span></td>
      <td>${{f.finding_id}}</td>
      <td><span style="background:${{c}};color:#fff;padding:2px 7px;border-radius:3px;font-size:.8em">${{f.risk_level}}</span></td>
      <td>${{f.module}}</td><td>${{f.category}}</td><td>${{f.summary}}</td>
    </tr>`;
  }});
}}

let sortDir = 1;
function sortTable(col) {{
  const tbody = document.getElementById('findings-body');
  const rows = Array.from(tbody.querySelectorAll('.finding-row'));
  rows.sort((a, b) => {{
    const va = a.cells[col]?.innerText || '';
    const vb = b.cells[col]?.innerText || '';
    return sortDir * va.localeCompare(vb, 'sv');
  }});
  sortDir *= -1;
  rows.forEach(row => {{
    tbody.appendChild(row);
    tbody.appendChild(row.nextElementSibling);
  }});
}}
</script>
</body>
</html>"""


def generate_dashboard(results: dict, output_path: str) -> None:
    """Skriver ett fristående HTML-dashboard till output_path.

    Args:
        results: Dict från generate_results_json.
        output_path: Fullständig sökväg till output-HTML-filen.
    """
    html_content = _html(results)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_content, encoding="utf-8")
    logger.info("Dashboard sparad: %s (%d bytes)", out, out.stat().st_size)
