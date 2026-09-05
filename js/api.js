// api.js
// Thin fetch wrapper for every racik.racik.api endpoint the frontend calls.
// Base URL is empty by default (the FastAPI app serves this frontend from
// the same origin); set window.RACIK_API_BASE before this module loads to
// point at a different host during local development.

const BASE = (typeof window !== 'undefined' && window.RACIK_API_BASE) || '';

async function request(method, path, body) {
  const res = await fetch(BASE + path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const payload = await res.json();
      detail = payload.detail || detail;
    } catch (_) { /* response wasn't JSON — keep statusText */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

export const api = {
  meta: () => request('GET', '/api/meta'),

  /**
   * Run the full Preprocessing -> Agen Gizi -> Agen Biaya -> CP-SAT ->
   * Validator pipeline for one calendar week (max 7 days) and persist it.
   */
  generate: (body) => request('POST', '/api/generate', body),

  /** Persist an accept/reject decision; `replace: true` also solves a
   * same-day replacement excluding the rejected dishes. */
  review: (body) => request('POST', '/api/review', body),

  /** Recently served dishes (menu history) and rejection reasons. */
  history: (limit = 30) => request('GET', `/api/history?limit=${limit}`),

  /** Record or update one ingredient's received/used/actual-cost/note. */
  stockUpsert: (body) => request('POST', '/api/stock', body),

  /** Every stock/audit record for one run. */
  stockList: (runId) => request('GET', `/api/stock?run_id=${runId}`),

  /** Full ingredient list, steps and gram provenance for one dish. */
  dish: (dishId) => request('GET', `/api/dish/${dishId}`),

  /** Short English glosses for a batch of Indonesian dish names — cached and
   * batched server-side, so calling this again with the same names is free. */
  translateDishNames: (names) => request('POST', '/api/translate', { names }),
};
