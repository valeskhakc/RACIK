// utils.js
// Small helpers used across every module: currency formatting, the toast
// notification, screen/tab visibility toggling, the reusable star-rating
// widget, and dish/ingredient name display (cleaning + English glossing).

import { state } from './state.js';

/** Format a number of Rupiah as "Rp 1,234,567". */
export function formatIDR(amount) {
  return `Rp ${Math.round(amount).toLocaleString('en-US')}`;
}

/** Format Rupiah as a signed delta, e.g. "+Rp 1,000" / "−Rp 1,000". */
export function formatIDRDelta(amount) {
  const sign = amount >= 0 ? '+' : '−';
  return `${sign}${formatIDR(Math.abs(amount))}`;
}

// A handful of staple/fruit reference dishes ("Nasi jagung", "Pisang", ...)
// exist in the corpus at several fixed reference sizes — "Nasi putih (100 g)",
// "(150 g)", "(200 g)", "(250 g)" — so the solver can pick whichever size
// best fits a day's targets. Which size gets picked varies day to day, which
// read as an inconsistency ("why does the same dish suddenly say 100g?") when
// that gram count is baked into the displayed name. The real portion size is
// already shown separately wherever it matters (ingredient list, ingredient
// mass); the headline name should stay stable.
const PORTION_SUFFIX = /\s*\(\s*\d+\s*g\w*\s*\)\s*$/i;

/** Strip a trailing "(100 g)"-style reference-portion suffix from a dish name. */
export function cleanDishName(name) {
  return String(name || '').replace(PORTION_SUFFIX, '').trim() || String(name || '');
}

/** The corpus (dish names and ingredient names alike) is entirely
 * Indonesian; a gloss is a short English aid shown alongside the real name,
 * never instead of it — the kitchen still shops and cooks from the
 * Indonesian name. Falls back to the cleaned Indonesian name until a gloss
 * is fetched (see plan.js's glossNames(), which populates state.dishGlosses
 * right after a plan is generated or a dish/ingredient list is opened). */
export function glossFor(name) {
  const clean = cleanDishName(name);
  return state.dishGlosses[clean] || clean;
}

// The 33 corpus provinces (see /api/meta) have fixed, standard English
// names — unlike dish/ingredient text, this is a closed, well-known set, so
// a static table is more correct and instant than routing it through an
// LLM gloss call. "Kalimantan" (no direction) is a real corpus value — an
// island-level fallback some rows use instead of a specific Kalimantan
// province — kept as-is rather than guessed at.
const PROVINCE_EN = {
  'aceh': 'Aceh', 'bali': 'Bali', 'banten': 'Banten',
  'bengkulu': 'Bengkulu', 'dki jakarta': 'Jakarta', 'di yogyakarta': 'Yogyakarta',
  'gorontalo': 'Gorontalo', 'jambi': 'Jambi', 'jawa barat': 'West Java',
  'jawa tengah': 'Central Java', 'jawa timur': 'East Java',
  'kalimantan': 'Kalimantan', 'kalimantan barat': 'West Kalimantan',
  'kalimantan selatan': 'South Kalimantan', 'kalimantan tengah': 'Central Kalimantan',
  'kalimantan timur': 'East Kalimantan', 'kalimantan utara': 'North Kalimantan',
  'kepulauan bangka belitung': 'Bangka Belitung Islands',
  'kepulauan riau': 'Riau Islands', 'lampung': 'Lampung', 'maluku': 'Maluku',
  'nusa tenggara barat': 'West Nusa Tenggara', 'nusa tenggara timur': 'East Nusa Tenggara',
  'papua': 'Papua', 'riau': 'Riau', 'sulawesi barat': 'West Sulawesi',
  'sulawesi selatan': 'South Sulawesi', 'sulawesi tengah': 'Central Sulawesi',
  'sulawesi tenggara': 'Southeast Sulawesi', 'sulawesi utara': 'North Sulawesi',
  'sumatera barat': 'West Sumatra', 'sumatera selatan': 'South Sumatra',
  'sumatera utara': 'North Sumatra', 'nasional': 'National',
};

/** English name for a corpus province (or "Nasional"/"National"). Provinces
 * are a closed, known set — unlike glossFor(), this never needs a network
 * call and never falls back to the raw Indonesian silently: an unrecognised
 * value (never expected, but data can surprise you) is returned unchanged
 * rather than guessed at. */
export function glossProvince(name) {
  const key = String(name || '').trim().toLowerCase();
  return PROVINCE_EN[key] || String(name || '');
}

/** Resolve free-typed or suggestion-picked location text to a corpus
 * province, or '' if nothing matches (the backend then plans with no
 * regional preference). Matches against both the Indonesian corpus name and
 * its English gloss, since text can arrive in either language — typed by
 * hand, or filled in from a suggestion shown in English. Shared by the
 * onboarding location picker and Settings' "Location" field, so both apply
 * the exact same matching rules. */
export function resolveProvince(text) {
  const low = (text || '').trim().toLowerCase();
  if (!low) return '';
  const found = state.provinces.find((p) => p.province.toLowerCase().includes(low)
      || glossProvince(p.province).toLowerCase().includes(low))
    || state.provinces.find((p) => low.includes(p.province.toLowerCase())
      || low.includes(glossProvince(p.province).toLowerCase()));
  return found ? found.province : '';
}

let toastTimer = null;

/** Show a brief toast notification at the bottom of the screen. */
export function toast(message) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2400);
}

/**
 * Show one `.screen` element within the onboarding flow (screen-1, screen-2)
 * or within the Plan tab (screen-3, screen-4), hiding all siblings.
 */
export function showScreen(id) {
  document.querySelectorAll('.screen').forEach((s) => s.classList.remove('active'));
  const el = document.getElementById(id);
  if (el) el.classList.add('active');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/** Update the onboarding stepper (Location / Parameters) to mark step `n` active. */
export function setOnboardingStep(n) {
  document.querySelectorAll('#onboardingStepper .step').forEach((s) => {
    const num = parseInt(s.dataset.step, 10);
    s.classList.toggle('active', num === n);
    s.classList.toggle('done', num < n);
  });
}

/**
 * Turn a "★★★★★" placeholder element into a clickable star-rating widget.
 * Call once per element at startup.
 */
export function setupStarRating(elementId) {
  const el = document.getElementById(elementId);
  if (!el) return;
  const chars = el.textContent.split('');
  el.innerHTML = chars.map((c, i) => `<span data-i="${i}">${c}</span>`).join('');
  el.addEventListener('click', (e) => {
    const span = e.target.closest('span');
    if (!span) return;
    const idx = parseInt(span.dataset.i, 10);
    el.querySelectorAll('span').forEach((s, i) => s.classList.toggle('on', i <= idx));
  });
}

/** Trigger a client-side CSV download from an array of already-formatted row strings. */
export function downloadCsv(header, rows, filename) {
  const blob = new Blob([header + rows.join('\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
  toast(`Downloaded ${filename}`);
}
