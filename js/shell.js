// shell.js
// The persistent sidebar shell that appears once a plan has been generated:
// tab switching between Meal Plan / Procurement / Stock & Audit / Settings,
// the header title+subtitle, and the "location · people · days" footer card.

import { state } from './state.js';
import { showScreen } from './utils.js';

const TAB_META = {
  plan: { title: 'Meal Plan', sub: "Review and approve this week's tray" },
  procurement: { title: 'Procurement', sub: 'Aggregated shopping list per day and per week' },
  inventory: { title: 'Stock & Audit', sub: 'Ingredient transparency, cost realization, and nutrition compliance' },
  settings: { title: 'Settings', sub: 'Program parameters and export options' },
};

/** Sync the sidebar's bottom summary card to the current location/people/day-count. */
export function updateSidebarFooter() {
  const loc = document.getElementById('sumLocation').textContent || '—';
  document.querySelector('#sidebarFooter .sf-loc').textContent = loc;
  document.querySelector('#sidebarFooter .sf-meta').textContent =
    `${state.people.toLocaleString('en-US')} beneficiaries · ${state.generatedDays.length} day${state.generatedDays.length === 1 ? '' : 's'}`;
}

/**
 * Switch from the onboarding screens to the persistent sidebar shell, then
 * do an initial render of every tab so Procurement/Inventory/Settings are
 * ready the moment someone clicks into them.
 *
 * @param {{
 *   populateProcurementSelectors: () => void,
 *   renderProcurement: () => void,
 *   populateInventorySelector: () => void,
 *   renderInventory: () => void,
 *   renderSettingsTab: () => void,
 * }} renderers
 */
export function enterAppShell(renderers) {
  document.getElementById('onboardingTopbar').style.display = 'none';
  document.getElementById('onboardingApp').style.display = 'none';
  document.getElementById('onboardingStepper').style.display = 'none';
  document.getElementById('appShell').style.display = 'flex';

  showScreen('screen-3');
  updateSidebarFooter();
  renderers.populateProcurementSelectors();
  renderers.renderProcurement();
  renderers.populateInventorySelector();
  renderers.renderInventory();
  renderers.renderSettingsTab();
}

/** Programmatically switch to a tab, as if its sidebar button was clicked. */
export function goToTab(tabName) {
  const btn = document.querySelector(`.nav-item[data-tab="${tabName}"]`);
  if (btn) btn.click();
}

/**
 * Wire up the four sidebar nav buttons. Each tab's renderer is only called
 * when that tab is actually opened, so nothing does wasted work up front.
 */
export function initShell(renderers) {
  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll('.nav-item').forEach((b) => b.classList.toggle('active', b === btn));
      document.querySelectorAll('.tab-panel').forEach((p) => p.classList.toggle('active', p.id === `tab-${tab}`));
      document.getElementById('panelTitle').textContent = TAB_META[tab].title;
      document.getElementById('panelSubtitle').textContent = TAB_META[tab].sub;
      window.scrollTo({ top: 0, behavior: 'smooth' });

      if (tab === 'procurement') {
        renderers.populateProcurementSelectors();
        renderers.renderProcurement();
      }
      if (tab === 'inventory') {
        renderers.populateInventorySelector();
        renderers.renderInventory();
      }
      if (tab === 'settings') {
        renderers.renderSettingsTab();
      }
    });
  });
}
