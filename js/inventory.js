// inventory.js
// Stock & Audit tab: real, persisted received/used/actual-cost records via
// /api/stock (racik.racik.statedb.StateStore.stock_records), replacing the
// mockup's Math.random() variance simulation. Planned quantities/costs come
// from the same per-day procurement breakdown the Procurement tab uses, so
// "planned" never disagrees between the two tabs.

import { state, estimateTotalBudget } from './state.js';
import { api } from './api.js';
import { MEETS_AKG_THRESHOLD, glossNames } from './plan.js';
import { formatIDR, downloadCsv, glossFor } from './utils.js';

// run_id -> stock records for that run (all days), fetched once and reused;
// invalidated (re-fetched) whenever this tab is (re)rendered after a save.
const recordsByRun = new Map();

async function loadRecordsForRun(runId) {
  if (!recordsByRun.has(runId)) {
    const res = await api.stockList(runId);
    recordsByRun.set(runId, res.records);
  }
  return recordsByRun.get(runId);
}

function recordFor(records, dayIndex, name) {
  return records.find((r) => r.day_index === dayIndex && r.ingredient_name === name);
}

export function populateInventorySelector() {
  const sel = document.getElementById('invDaySelect');
  sel.innerHTML = state.generatedDays.map((d) => `<option value="${d.key}">${d.name}</option>`).join('');
  sel.onchange = renderInventory;
}

function selectedDay() {
  const key = document.getElementById('invDaySelect').value || (state.generatedDays[0] || {}).key;
  return state.generatedDays.find((d) => d.key === key);
}

async function dayActualCost(day) {
  if (!day || !day.procurement) return 0;
  const records = await loadRecordsForRun(day.runId);
  const dayRecords = records.filter((r) => r.day_index === day.dayIndex);
  if (dayRecords.length === 0) return 0;   // nothing recorded yet for this day —
  // falling back to the planned total here would make an untouched day's
  // "Actual" bar match its "Planned" bar exactly, contradicting the chart's
  // own "bars at zero mean that day hasn't been recorded yet" caption.
  const lines = day.procurement.lines;
  let total = 0;
  for (const line of lines) {
    const rec = recordFor(dayRecords, day.dayIndex, line.name);
    // An ingredient with no record yet, on a day that's partway through
    // being audited, is estimated at its planned cost — a reasonable
    // in-progress figure, unlike a whole untouched day (handled above).
    total += rec && rec.actual_cost_idr != null ? rec.actual_cost_idr : line.cost_idr;
  }
  return total;
}

function renderBarChart(container, days, actualByKey) {
  const maxCost = Math.max(1, ...days.map((d) => Math.max(d.procurement.totals.total_cost_idr,
    actualByKey.get(d.key) || 0)));
  container.innerHTML = `
    <div class="chart-legend">
      <span><span class="dot plan"></span>Planned</span>
      <span><span class="dot actual"></span>Actual</span>
    </div>
    <div class="bar-chart">
      ${days.map((d) => {
        const planned = d.procurement.totals.total_cost_idr;
        const actual = actualByKey.get(d.key) || 0;
        const planH = Math.round((planned / maxCost) * 150);
        const actH = actual > 0 ? Math.round((actual / maxCost) * 150) : 0;
        return `
          <div class="bar-group">
            <div class="bars">
              <div class="bar plan" style="height:${planH}px" title="Planned ${formatIDR(planned)}"></div>
              <div class="bar actual" style="height:${actH}px" title="Actual ${formatIDR(actual)}"></div>
            </div>
            <span class="bar-label">${d.name.slice(0, 3)}</span>
          </div>`;
      }).join('')}
    </div>`;
}

function renderNutritionMini(container, day) {
  const rows = (day.compliance || []).filter((c) => c.binding);
  container.innerHTML = rows.map((c) => `
    <div class="nutri-item">
      <span>${c.label_en}${c.verified ? '' : ' (info)'}</span>
      <span style="display:flex;align-items:center;">${c.value}${c.unit} / ${c.target}${c.unit}
        <span class="bar-track"><span class="bar-fill" style="width:${Math.min(100, c.pct)}%"></span></span>
      </span>
    </div>`).join('');
}

async function renderStockTable(day) {
  const tableEl = document.getElementById('invTable');
  if (!day || !day.procurement) { tableEl.innerHTML = ''; return; }
  const records = await loadRecordsForRun(day.runId);
  const lines = day.procurement.lines;
  await glossNames(lines.map((l) => l.name));

  const rowsHtml = lines.map((line) => {
    const rec = recordFor(records, day.dayIndex, line.name) || {
      received_pct: 100, used_pct: 100, actual_cost_idr: line.cost_idr, note: '',
    };
    const actual = rec.actual_cost_idr != null ? rec.actual_cost_idr : line.cost_idr;
    const varianceIdr = actual - line.cost_idr;
    const varianceClass = varianceIdr > 0 ? 'bad' : 'good';
    return `
      <tr data-name="${line.name}">
        <td>${glossFor(line.name)}</td>
        <td class="num">${line.gross_kg.toFixed(2)} kg</td>
        <td class="num">${formatIDR(line.cost_idr)}</td>
        <td class="num"><input type="number" class="inv-received" min="0" max="200" value="${rec.received_pct}"> %</td>
        <td class="num"><input type="number" class="inv-used" min="0" max="200" value="${rec.used_pct}"> %</td>
        <td class="num">
          <input type="number" class="inv-actual" min="0" value="${Math.round(actual)}">
          <span class="variance-note ${varianceClass}">${varianceIdr >= 0 ? '+' : ''}${formatIDR(varianceIdr)}</span>
        </td>
        <td><input type="text" class="wide inv-note" value="${rec.note || ''}" placeholder="Note"></td>
      </tr>`;
  }).join('');

  tableEl.innerHTML = `
    <thead><tr>
      <th>Ingredient</th><th class="num">Planned qty</th><th class="num">Planned cost</th>
      <th class="num">Received</th><th class="num">Used</th><th class="num">Actual cost</th><th>Note</th>
    </tr></thead>
    <tbody>${rowsHtml}</tbody>`;

  tableEl.querySelectorAll('tr[data-name]').forEach((row) => {
    const name = row.dataset.name;
    const save = () => {
      const received = parseFloat(row.querySelector('.inv-received').value) || 0;
      const used = parseFloat(row.querySelector('.inv-used').value) || 0;
      const actual = parseFloat(row.querySelector('.inv-actual').value) || 0;
      const note = row.querySelector('.inv-note').value;
      api.stockUpsert({
        run_id: day.runId, day_index: day.dayIndex, ingredient_name: name,
        received_pct: received, used_pct: used, actual_cost_idr: actual, note,
      }).then(() => {
        recordsByRun.delete(day.runId);   // force a fresh read next render
        renderInventory();
      }).catch((err) => console.warn('Could not save stock record:', err));
    };
    row.querySelectorAll('input').forEach((input) => {
      input.addEventListener('change', save);
    });
  });
}

export async function renderInventory() {
  if (!state.generatedDays.length) return;
  const day = selectedDay();
  if (!day) return;

  const totalDays = state.generatedDays.length;
  const budgetCeiling = estimateTotalBudget(totalDays);
  const plannedTotal = state.generatedDays.reduce(
    (sum, d) => sum + (d.procurement ? d.procurement.totals.total_cost_idr : 0), 0);

  const actualByKey = new Map();
  for (const d of state.generatedDays) {
    actualByKey.set(d.key, await dayActualCost(d));
  }
  const actualTotal = [...actualByKey.values()].reduce((s, v) => s + v, 0);
  const recordedAny = [...recordsByRun.values()].some((rs) => rs.length > 0);
  const actualTotalDisplay = recordedAny ? actualTotal : 0;

  document.getElementById('invBudget').textContent = formatIDR(budgetCeiling);
  document.getElementById('invPlanned').textContent = formatIDR(plannedTotal);
  document.getElementById('invActual').textContent = formatIDR(actualTotalDisplay);
  document.getElementById('invActualPct').textContent =
    `${plannedTotal ? Math.round((actualTotalDisplay / plannedTotal) * 100) : 0}% of plan`;
  const variance = actualTotalDisplay - plannedTotal;
  document.getElementById('invVariance').textContent = formatIDR(variance);
  document.getElementById('invVarianceHint').textContent = recordedAny
    ? (variance > 0 ? 'Over plan' : 'Under or on plan')
    : 'No records entered yet';

  renderBarChart(document.getElementById('invChart'), state.generatedDays, actualByKey);

  const daysMet = state.generatedDays.filter((d) => d.adequacy >= MEETS_AKG_THRESHOLD).length;
  document.getElementById('invComplianceSub').textContent =
    `${daysMet} of ${totalDays} days meet all enforced AKG criteria.`;
  document.getElementById('invComplianceBar').style.width =
    `${totalDays ? Math.round((daysMet / totalDays) * 100) : 0}%`;
  renderNutritionMini(document.getElementById('invNutritionMini'), day);

  await renderStockTable(day);
}

/** Full stock/audit CSV across every generated day — used by both the
 * Inventory tab's own export button and Settings' "Export to Excel/CSV". */
export async function exportAuditCsv() {
  const header = 'Day,Ingredient,Planned Cost,Actual Cost,Received %,Used %,Note\n';
  const rows = [];
  for (const d of state.generatedDays) {
    if (!d.procurement) continue;
    const records = await loadRecordsForRun(d.runId);
    for (const line of d.procurement.lines) {
      const rec = recordFor(records, d.dayIndex, line.name) || {
        received_pct: 100, used_pct: 100, actual_cost_idr: line.cost_idr, note: '',
      };
      const actual = rec.actual_cost_idr != null ? rec.actual_cost_idr : line.cost_idr;
      rows.push(`"${d.name}","${glossFor(line.name)}",${Math.round(line.cost_idr)},${Math.round(actual)},` +
        `${rec.received_pct},${rec.used_pct},"${rec.note || ''}"`);
    }
  }
  downloadCsv(header, rows, 'racik-audit.csv');
}

export function initInventoryTab() {
  document.getElementById('invExportBtn').addEventListener('click', exportAuditCsv);
}
