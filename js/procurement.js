// procurement.js
// Procurement tab: weekly and daily shopping-list recaps, sourced from the
// procurement breakdown /api/generate already computed against the exact
// solved plan (racik.racik.orchestrator.RacikOrchestrator._procurement_payload)
// — never re-aggregated client-side, so it can't drift from the tray shown
// on the Plan tab.

import { state } from './state.js';
import { formatIDR, downloadCsv, cleanDishName, glossFor } from './utils.js';
import { glossNames } from './plan.js';

function currentWeek() {
  const idx = parseInt(document.getElementById('procWeekSelect').value, 10) || 0;
  return state.weeks[idx];
}

function daysInWeek(weekIndex) {
  const week = state.weeks[weekIndex];
  if (!week) return [];
  return state.generatedDays.filter((d) => d.runId === week.runId);
}

export function populateProcurementSelectors() {
  const weekSel = document.getElementById('procWeekSelect');
  weekSel.innerHTML = state.weeks
    .map((w, i) => `<option value="${i}">Week ${w.week}</option>`)
    .join('');

  const daySel = document.getElementById('procDaySelect');
  const fillDaySelect = () => {
    const weekIdx = parseInt(weekSel.value, 10) || 0;
    daySel.innerHTML = daysInWeek(weekIdx)
      .map((d) => `<option value="${d.key}">${d.name}</option>`)
      .join('');
  };
  fillDaySelect();

  weekSel.onchange = () => { fillDaySelect(); renderProcurement(); };
  daySel.onchange = renderProcurement;
}

function renderTable(tableEl, lines, totalCostIdr) {
  const rows = lines.map((l) => {
    const gloss = glossFor(l.name);
    const original = cleanDishName(l.name);
    const nameCell = gloss !== original
      ? `${gloss}<span class="ing-name-id" style="display:block;font-size:11.5px;color:var(--ink-soft);margin-top:1px;">${original}</span>`
      : gloss;
    return `
    <tr>
      <td>${nameCell}</td>
      <td class="num">${l.gross_kg.toFixed(2)}</td>
      <td class="num">${Math.round(l.cost_idr).toLocaleString('en-US')}</td>
      <td>${(l.used_in || []).map(glossFor).join(', ')}</td>
    </tr>`;
  }).join('');
  // Units live in the header (Weight (kg), Cost (Rp)) instead of repeating
  // on every row — the whole column is the same unit, so saying it once
  // reads cleaner than "12.50 kg" fifty times down a shopping list.
  tableEl.innerHTML = `
    <thead><tr><th>Ingredient</th><th class="num">Weight (kg)</th><th class="num">Cost (Rp)</th><th>Used in</th></tr></thead>
    <tbody>${rows}
      <tr class="total-row"><td>Total</td><td></td><td class="num">${Math.round(totalCostIdr).toLocaleString('en-US')}</td><td></td></tr>
    </tbody>`;
}

function csvFor(lines) {
  const header = 'Ingredient,Weight (kg),Cost (IDR),Used in\n';
  const rows = lines.map((l) =>
    `"${glossFor(l.name)}",${l.gross_kg.toFixed(3)},${Math.round(l.cost_idr)},"${(l.used_in || []).map(glossFor).join('; ')}"`);
  return { header, rows };
}

export async function renderProcurement() {
  if (!state.weeks.length) return;
  const week = currentWeek();
  if (!week || !week.procurement) return;

  // Dish names (used_in) are already glossed by the time Procurement is
  // visible — buildTray() glosses them right after generating. Ingredient
  // names are new to this tab, so gloss them here, once per week's lines.
  await glossNames(week.procurement.lines.map((l) => l.name));

  const weekDays = daysInWeek(parseInt(document.getElementById('procWeekSelect').value, 10) || 0);
  document.getElementById('procWeekTitle').textContent =
    `Ingredient needs, Week ${week.week} · ${weekDays.length} days × ${state.people} portions`;
  document.getElementById('procWeekSub').textContent =
    `${week.procurement.totals.items} ingredients · ${formatIDR(week.procurement.totals.total_cost_idr)} estimated total`;
  renderTable(document.getElementById('procWeekTable'), week.procurement.lines,
    week.procurement.totals.total_cost_idr);

  document.getElementById('procWeekCsv').onclick = () => {
    const { header, rows } = csvFor(week.procurement.lines);
    downloadCsv(header, rows, `racik-procurement-week${week.week}.csv`);
  };

  const dayKey = document.getElementById('procDaySelect').value;
  const day = state.generatedDays.find((d) => d.key === dayKey) || weekDays[0];
  if (day && day.procurement) {
    document.getElementById('procDayTitle').textContent = `Ingredient needs, ${day.name}`;
    document.getElementById('procDaySub').textContent =
      `${day.procurement.totals.items} ingredients · ${formatIDR(day.procurement.totals.total_cost_idr)} estimated total`;
    renderTable(document.getElementById('procDayTable'), day.procurement.lines,
      day.procurement.totals.total_cost_idr);
    document.getElementById('procDayCsv').onclick = () => {
      const { header, rows } = csvFor(day.procurement.lines);
      downloadCsv(header, rows, `racik-procurement-${day.key}.csv`);
    };
  }
}

export function initProcurementTab() {
  document.querySelectorAll('.subtab').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.subtab').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      document.querySelectorAll('.subtab-panel').forEach((p) => p.classList.remove('active'));
      document.getElementById(`proc-${btn.dataset.subtab}`).classList.add('active');
    });
  });
}
