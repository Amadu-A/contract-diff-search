// app/web/static/js/api.js
/** Единая работа с API, безопасными ошибками, cookie и CSRF-заголовком. */
export async function apiRequest(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'ContractDiff', ...options.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === 'string' ? body.detail : `Ошибка запроса (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}

/** Строит только относительные адреса текущего задания без секретов в URL. */
export function jobUrl(id, suffix = '') {
  return `/api/v1/comparisons/${encodeURIComponent(id)}${suffix}`;
}
