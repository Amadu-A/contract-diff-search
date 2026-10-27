// app/web/static/js/app.js
/** Сборка frontend-модулей и жизненный цикл одного временного сравнения. */
import { apiRequest, jobUrl } from './api.js';
import { terminalStatus } from './state.js';
import { UploadCard } from './features/upload.js';
import { Viewer } from './features/viewer.js';
import { Results } from './features/results.js';
import { AIProgress } from './features/ai-progress.js';

const form = document.querySelector('[data-comparison-form]');
const start = document.querySelector('[data-start]');
const cancel = document.querySelector('[data-cancel]');
const progress = document.querySelector('[data-progress]');
let currentJob = null;
let generation = 0;
let pollTimer;
let expires = 0;
let busy = false;
let sync = true;
let toastTimer;
let displayedJob = null;
let lastAIRevision = '';

/** Показывает понятную ошибку и не раскрывает технических ответов сервера. */
function showError(error) {
  const toast = document.querySelector('[data-toast]');
  toast.textContent = error.message || String(error);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 9000);
}

/** Записывает только реальные события текущего задания в панель уведомлений. */
function event(message) {
  const list = document.querySelector('[data-events]');
  if (!list.dataset.started) { list.replaceChildren(); list.dataset.started = 'true'; }
  const item = document.createElement('li');
  item.textContent = `${new Date().toLocaleTimeString('ru-RU')} · ${message}`;
  list.prepend(item);
  while (list.childElementCount > 50) list.lastElementChild.remove();
  document.querySelector('[data-notification-dot]').hidden = false;
}

/** Синхронизирует ручной переход только по подтверждённому соответствию страниц. */
async function synchronize(side, page) {
  if (!sync || !currentJob) return;
  try {
    const mapping = await apiRequest(jobUrl(currentJob.id, `/correspondence/${side}/${page}`));
    if (mapping.page) await viewers.find(viewer => viewer.side !== side).go(mapping.page);
  } catch (error) { showError(error); }
}

const viewers = [...document.querySelectorAll('[data-viewer]')].map(root => new Viewer(root, synchronize, showError));
const results = new Results(viewers, showError);
const aiProgress = new AIProgress(showError, retryAI);
document.querySelector('[data-show-unverified-toggle]').addEventListener('change', event => results.setVisibility(event.target.checked).catch(showError));

/** Загружает checkpoint, сохраняя выбор и прокрутку при поступлении результатов ИИ. */
async function displayResult(job, run, quiet = false) {
  const summary = await apiRequest(jobUrl(job.id, '/result'));
  if (run !== generation) return;
  if (displayedJob !== job.id) {
    await Promise.all(viewers.map(viewer => viewer.setDocument(job.id, summary.documents[viewer.side], summary.exports[viewer.side])));
    displayedJob = job.id;
  } else {
    viewers.forEach(viewer => { viewer.exports = summary.exports[viewer.side]; viewer.updateExports(); });
    await Promise.all(viewers.map(viewer => viewer.refreshRegions()));
  }
  await results.setResult(job.id, summary, quiet);
  aiProgress.update(job, summary.ai_progress, summary.verification_metrics);
  document.querySelector('[data-delete]').hidden = false;
  return summary;
}

/** Повторяет сохранённые незавершённые операции, сохраняя уже извлечённые документы. */
async function retryAI() {
  if (!currentJob || busy) return;
  const run = ++generation;
  currentJob = await apiRequest(jobUrl(currentJob.id, '/ai/retry'), {method:'POST'});
  setBusy(true);
  progress.hidden = false;
  progress.classList.remove('progress--done');
  poll(currentJob.id, run);
}

/** Пересчитывает допустимость запуска после проверки карточек. */
function updateFiles() {
  start.disabled = busy || cards.some(card => !card.file);
}
const cards = [...document.querySelectorAll('[data-upload]')].map(root => new UploadCard(root, Number(form.dataset.maxFileMb), updateFiles));

/** Блокирует конфликтующие действия, оставляя доступной отмену и сброс. */
function setBusy(value) {
  busy = value;
  cards.forEach(card => card.setBusy(value));
  for (const control of document.querySelectorAll('[data-ai-full],[data-ai-unverified]')) control.disabled = value;
  start.hidden = value;
  cancel.hidden = !value;
  updateFiles();
}

/** Опрашивает сохранённый статус, останавливаясь на любом конечном состоянии. */
async function poll(id, run) {
  try {
    const job = await apiRequest(jobUrl(id));
    if (run !== generation) return;
    if (currentJob?.stage !== job.stage) event(job.stage);
    currentJob = job;
    aiProgress.update(job);
    document.querySelector('[data-stage]').textContent = job.stage;
    expires = job.expires;
    if (!terminalStatus(job.status)) {
      const revision = JSON.stringify(job.ai_progress || {});
      if (Object.keys(job.ai_progress || {}).length && revision !== lastAIRevision) {
        await displayResult(job, run, true);
        lastAIRevision = revision;
      }
      pollTimer = setTimeout(() => poll(id, run), 1200);
      return;
    }
    setBusy(false);
    if (['completed','completed_with_warnings'].includes(job.status) || (displayedJob === id && ['cancelled','failed'].includes(job.status))) {
      const summary = await displayResult(job, run, displayedJob === id);
      if (run !== generation) return;
      progress.classList.add('progress--done');
      document.querySelector('[data-stage]').textContent = job.status === 'cancelled' ? 'Проверка отменена; сохранён частичный результат' : job.status === 'completed' ? 'Сравнение завершено' : 'Сравнение завершено с предупреждениями';
      document.querySelector('[data-ai-status]').textContent = ({not_requested:'Без ИИ-проверки',completed:'Запланированные ИИ-операции выполнены',partial:'ИИ-проверка частичная; основной результат сохранён',running:'ИИ-проверка продолжается',failed:'ИИ-проверка завершилась ошибкой; основной результат сохранён'})[summary.ai_status] || '';
      document.querySelector('[data-delete]').hidden = false;
      event('Результат и доступные экспорты готовы');
    } else {
      document.querySelector('[data-stage]').textContent = job.error || (job.status === 'cancelled' ? 'Сравнение отменено' : 'Задание недоступно');
      if (job.error) showError(new Error(job.error));
      event(job.error || job.status);
    }
  } catch (error) {
    if (run !== generation) return;
    setBusy(false);
    showError(error);
    document.querySelector('[data-stage]').textContent = 'Не удалось получить состояние задания';
  }
}

/** Отправляет выбранные файлы и запускает polling; никаких фиктивных процентов нет. */
form.addEventListener('submit', async submitEvent => {
  submitEvent.preventDefault();
  if (busy || cards.some(card => !card.file)) return;
  const run = ++generation;
  clearTimeout(pollTimer);
  results.clear();
  aiProgress.clear();
  displayedJob = null;
  lastAIRevision = '';
  viewers.forEach(viewer => viewer.clear());
  setBusy(true);
  progress.hidden = false;
  progress.classList.remove('progress--done');
  document.querySelector('[data-stage]').textContent = 'Передача и проверка файлов';
  document.querySelector('[data-ai-status]').textContent = '';
  try {
    if (currentJob) await apiRequest(jobUrl(currentJob.id), {method:'DELETE'}).catch(() => {});
    const body = new FormData();
    cards.forEach(card => body.append(card.root.dataset.upload, card.file));
    body.append('ai_full', String(document.querySelector('[data-ai-full]').checked));
    body.append('ai_unverified', String(document.querySelector('[data-ai-unverified]').checked));
    const job = await apiRequest('/api/v1/comparisons', {method:'POST', body});
    if (run !== generation) {
      await apiRequest(jobUrl(job.id), {method:'DELETE'});
      return;
    }
    currentJob = job;
    event('Документы приняты, задание поставлено в очередь');
    poll(job.id, run);
  } catch (error) {
    if (run !== generation) return;
    setBusy(false);
    showError(error);
    document.querySelector('[data-stage]').textContent = 'Файлы не приняты';
  }
});

/** Сбрасывает данные и прекращает доступ к предыдущему результату на сервере. */
async function reset() {
  ++generation;
  clearTimeout(pollTimer);
  const previous = currentJob;
  currentJob = null;
  expires = 0;
  results.clear();
  aiProgress.clear();
  displayedJob = null;
  lastAIRevision = '';
  viewers.forEach(viewer => viewer.clear());
  cards.forEach(card => card.setFile(null, false));
  document.querySelector('[data-ai-full]').checked = false;
  document.querySelector('[data-ai-unverified]').checked = false;
  document.querySelector('[data-show-unverified-toggle]').checked = true;
  await results.setVisibility(true);
  document.querySelector('[data-delete]').hidden = true;
  progress.hidden = true;
  setBusy(false);
  if (previous) {
    await apiRequest(jobUrl(previous.id), {method:'DELETE'}).catch(showError);
    event('Результат удалён, доступ закрыт');
  }
}

document.querySelector('[data-reset]').addEventListener('click', reset);
document.querySelector('[data-delete]').addEventListener('click', reset);
cancel.addEventListener('click', async () => {
  if (!currentJob) return;
  try { await apiRequest(jobUrl(currentJob.id, '/cancel'), {method:'POST'}); event('Отмена запрошена'); }
  catch (error) { showError(error); }
});
document.querySelector('[data-sync]').addEventListener('click', event => {
  sync = !sync;
  event.currentTarget.setAttribute('aria-pressed', String(sync));
  event.currentTarget.title = sync ? 'Связанные переходы включены' : 'Связанные переходы выключены';
});
document.querySelector('[data-help]').addEventListener('click', () => document.querySelector('[data-help-dialog]').showModal());
document.querySelector('[data-notifications]').addEventListener('click', () => {
  document.querySelector('[data-events-dialog]').showModal();
  document.querySelector('[data-notification-dot]').hidden = true;
});
setInterval(() => {
  if (!expires || !currentJob) return;
  const seconds = Math.max(0, Math.ceil(expires - Date.now() / 1000));
  if (!busy) document.querySelector('[data-retention]').textContent = `◷ Файлы будут удалены через ${Math.ceil(seconds / 60)} мин.`;
  if (!seconds) {
    ++generation;
    clearTimeout(pollTimer);
    viewers.forEach(viewer => viewer.clear());
    results.clear();
    aiProgress.clear();
    displayedJob = null;
    setBusy(false);
    currentJob = null;
    document.querySelector('[data-retention]').textContent = 'Срок хранения истёк. Загрузите документы повторно.';
  }
}, 1000);
