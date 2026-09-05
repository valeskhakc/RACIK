// settings.js
// Program parameters recap and the feedback log (real, persisted rejection
// reasons from /api/history, not just this session's in-memory pushes).
// PDF/image export live on the Meal Plan tab now (js/plan.js); CSV export
// lives on the Procurement tab (js/procurement.js).

import { state, resetForRegenerate } from './state.js';
import { api } from './api.js';
import { toast, glossProvince, resolveProvince } from './utils.js';

function friendlyDayName(run_id, day_index) {
  const match = state.generatedDays.find((d) => d.runId === run_id && d.dayIndex === day_index);
  return match ? match.name : `Run #${run_id} · Day ${day_index + 1}`;
}

/** Reflect the budget-mode toggle's active button in state.budgetMode. */
function setBudgetModeButtons(mode) {
  document.querySelectorAll('#setBudgetMode button').forEach((b) => {
    b.classList.toggle('active', (b.dataset.mode === 'per') === (mode === 'per_portion'));
  });
}

/** Populate the editable parameter fields and the feedback log with the
 * actual values currently in state — not a derived/computed figure that
 * only coincidentally resembles what the operator typed. Budget in
 * particular used to always show a computed *total* even when the operator
 * had entered a per-portion figure, so "Rp 3,500,000" appeared for someone
 * who only ever typed "14,000" — this shows state.budgetValue verbatim,
 * with the toggle reflecting which unit it's actually in. */
export async function renderSettingsTab() {
  document.getElementById('setLocation').value =
    state.province ? glossProvince(state.province) : state.location;
  document.getElementById('setPeople').value = state.people;
  document.getElementById('setBudget').value = Math.round(state.budgetValue).toLocaleString('en-US');
  setBudgetModeButtons(state.budgetMode);
  document.getElementById('setDuration').value =
    ['3', '5', '7'].includes(state.selectedDuration) ? state.selectedDuration : 'custom';

  const list = document.getElementById('settingsFeedbackList');
  let rejections = [];
  try {
    const history = await api.history();
    rejections = history.rejections.map((r) => ({
      day: friendlyDayName(r.run_id, r.day_index), reason: r.reason,
    }));
  } catch (err) {
    // fall back to this session's optimistic log if the server is unreachable
    rejections = state.rejectionLog;
  }
  if (rejections.length === 0) {
    list.innerHTML = '<p class="empty-note">No rejections recorded yet.</p>';
  } else {
    list.innerHTML = rejections
      .map((r) => `<div class="fb-item"><span class="fb-day">${r.day}</span><span class="fb-reason">${r.reason}</span></div>`)
      .join('');
  }
}

/**
 * Wire up the Settings tab. `callbacks` lets this module trigger a full
 * re-render of the other tabs after "Regenerate Plan" without importing
 * them directly (keeps the module graph a simple tree, not a web).
 *
 * @param {{
 *   updatePeopleDisplay: () => void,
 *   buildTray: () => Promise<void>,
 *   refreshProcurement: () => void,
 *   refreshInventory: () => void,
 *   updateSidebarFooter: () => void,
 *   goToPlanTab: () => void,
 * }} callbacks
 */
export function initSettingsTab(callbacks) {
  // Visual only — like onboarding's own budget toggle, picking a mode here
  // doesn't touch state.budgetMode until "Apply & Regenerate" reads it, so
  // nothing about the live plan changes just from clicking around this tab.
  document.querySelectorAll('#setBudgetMode button').forEach((b) => {
    b.addEventListener('click', () => setBudgetModeButtons(b.dataset.mode === 'per' ? 'per_portion' : 'total'));
  });

  document.getElementById('setLocation').addEventListener('input', (e) => {
    e.target.classList.remove('input-error');
  });

  const regenerateBtn = document.getElementById('settingsRegenerate');
  regenerateBtn.addEventListener('click', async () => {
    // Without this, a second click while a regenerate is still in flight
    // races its own resetForRegenerate()/buildTray() against the first's —
    // whichever resetForRegenerate() runs last can clear state.generatedDays
    // after the other call already returned, leaving the tray permanently
    // empty ("0 days") with no error, since both requests genuinely
    // succeeded server-side. Disabling the button for the duration removes
    // the race instead of trying to detect it after the fact.
    if (regenerateBtn.disabled) return;
    regenerateBtn.disabled = true;

    // Every field is read and applied here, in this one place, only on this
    // one click — nothing regenerates from just editing a field. Previously
    // only "Number of people" actually did anything: editing location,
    // budget, or duration and clicking regenerate silently kept the old
    // values, which is exactly backwards from what "these values were set
    // during onboarding" implies a reader should be able to do.
    const newPeople = parseInt(document.getElementById('setPeople').value, 10);
    if (newPeople) state.people = newPeople;

    // An edited location that doesn't resolve to a real corpus province used
    // to silently regenerate with no regional preference at all — the same
    // "planned without a location" gap onboarding had. Block the regenerate
    // instead, same as onboarding's own location field now does.
    const locationText = document.getElementById('setLocation').value.trim();
    if (locationText) {
      const resolvedProvince = resolveProvince(locationText);
      if (!resolvedProvince) {
        toast("Couldn't match that location to a province, pick one, or leave it as is.");
        document.getElementById('setLocation').classList.add('input-error');
        regenerateBtn.disabled = false;
        return;
      }
      document.getElementById('setLocation').classList.remove('input-error');
      state.location = locationText;
      state.province = resolvedProvince;
    }

    const newBudget = parseFloat(document.getElementById('setBudget').value.replace(/[^\d.]/g, ''));
    if (Number.isFinite(newBudget) && newBudget > 0) state.budgetValue = newBudget;
    const perActive = document.querySelector('#setBudgetMode button[data-mode="per"]').classList.contains('active');
    state.budgetMode = perActive ? 'per_portion' : 'total';

    const durationChoice = document.getElementById('setDuration').value;
    if (durationChoice !== 'custom') state.selectedDuration = durationChoice;
    // 'custom' intentionally leaves state.selectedDuration/customSelectedDays/
    // customWeeks untouched — editing a custom day-of-week pick isn't
    // exposed in this compact form, only from onboarding itself.

    callbacks.updatePeopleDisplay();
    document.getElementById('sumLocation').textContent =
      state.province ? glossProvince(state.province) : state.location;

    resetForRegenerate();
    try {
      await callbacks.buildTray();
      callbacks.refreshProcurement();
      callbacks.refreshInventory();
      callbacks.updateSidebarFooter();
      callbacks.goToPlanTab();
      toast('Plan regenerated with updated settings.');
    } catch (err) {
      toast(`Couldn't regenerate: ${err.message}`);
    } finally {
      regenerateBtn.disabled = false;
    }
  });
}
