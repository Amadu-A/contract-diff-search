// app/web/static/js/features/ai-progress.js
/** Независимые фактические статусы режимов и технические результаты без фиктивного процента. */
import { apiRequest, jobUrl } from '../api.js';

const labels = {not_requested:'Не запрошена',queued:'Ожидает выполнения',running:'Выполняется',completed:'Завершена',partial:'Завершена частично',failed:'Ошибка',cancelled:'Отменена'};
const errors = {timeout:'Таймаут запроса',network:'Ошибка связи',http_error:'Ошибка HTTP',context_limit:'Лимит контекста',output_truncated:'Исчерпан лимит ответа',empty_content:'Пустой ответ',invalid_json:'Некорректный JSON',json_validation:'Неверная структура ответа',context_incomplete:'Не вся область проверена',unsupported_parameter:'Несовместимый параметр',time_budget:'Исчерпано время проверки',internal:'Внутренняя ошибка'};

export class AIProgress {
  /** Подключает только реальные технические сведения и повтор незавершённых операций. */
  constructor(onError, onRetry, readOnly = false) {
    this.onError = onError;
    this.readOnly = readOnly;
    this.sequence = 0;
    this.root = document.querySelector('[data-ai-progress]');
    this.operations = document.querySelector('[data-ai-operations]');
    document.querySelector('[data-ai-details]').addEventListener('click', () => this.details().catch(onError));
    document.querySelector('[data-ai-retry]').addEventListener('click', () => onRetry().catch(onError));
  }

  /** Обновляет счётчики из сохранённого backend checkpoint; неизвестное не считается выполненным. */
  update(job, reports = job.ai_progress || {}, metrics = {}) {
    this.job = job;
    this.root.hidden = !job.ai_full && !job.ai_unverified;
    for (const [mode, title] of [['full','Полная проверка документов'],['unverified','Проверка непроверенных областей']]) {
      const report = reports[mode] || {};
      const status = report.status || (job[`ai_${mode}`] ? 'queued' : 'not_requested');
      const pages = ['old','new'].reduce((count, side) => count + (report.pages_processed?.[side]?.length || 0),0);
      const totalPages = ['old','new'].reduce((count, side) => count + (report.pages_planned?.[side]?.length || 0),0);
      document.querySelector(`[data-ai-mode-${mode}]`).textContent = `${title}: ${labels[status] || status}. Операции: ${report.processed || 0}/${report.planned || 0}; успешно: ${report.succeeded || 0}; ошибок: ${report.failed || 0}; не обработано: ${report.remaining || 0}. Обработано страниц: ${pages}/${totalPages}.`;
    }
    const source = metrics.original_unverified ?? reports.original_unverified;
    const resolved = metrics.resolved_ai ?? reports.resolved_ai;
    document.querySelector('[data-ai-resolution]').textContent = source !== undefined ? `Исходных непроверенных областей: ${source}; разрешено ИИ: ${resolved || 0}${metrics.new_unverified ? `; новых: ${metrics.new_unverified}` : ''}${metrics.remaining_unverified !== undefined ? `; осталось: ${metrics.remaining_unverified}` : ''}.` : '';
    document.querySelector('[data-ai-retry]').hidden = this.readOnly || !['completed','completed_with_warnings','cancelled','failed'].includes(job.status) || (job.status !== 'cancelled' && !Object.values(reports).some(value => value && ['failed','partial'].includes(value.status)));
  }

  /** Показывает состояния отдельных единиц и безопасные категории ошибок. */
  async details(offset = 0) {
    if (!this.job) return;
    if (!offset) {
      this.sequence += 1;
      this.operations.hidden = !this.operations.hidden;
      this.operations.replaceChildren();
    }
    if (this.operations.hidden) return;
    const sequence = this.sequence;
    const id = this.job.id;
    const data = await apiRequest(jobUrl(id, `/ai/operations?offset=${offset}&limit=100`));
    if (this.sequence !== sequence || this.job?.id !== id || this.operations.hidden) return;
    for (const item of data.items) {
      const line = document.createElement('p');
      const accepted = item.verified_by ? (item.outcome === 'same' ? 'Совпадает — проверено ИИ' : 'Различие подтверждено ИИ') : item.status;
      line.textContent = `${item.mode} · ${item.kind} · ${item.id} · ${accepted}${item.error_kind ? ` · ${errors[item.error_kind] || 'Ошибка проверки'}` : ''} · попыток: ${item.attempts} · ${Math.round(item.duration_ms)} мс`;
      this.operations.append(line);
    }
    const next = offset + data.items.length;
    if (next < data.total && data.items.length) {
      const more = document.createElement('button');
      more.className = 'button';
      more.type = 'button';
      more.textContent = `Следующие операции (${next}/${data.total})`;
      more.addEventListener('click', () => { more.remove(); this.details(next).catch(this.onError); });
      this.operations.append(more);
    }
  }

  /** Очищает состояние после удаления сравнения. */
  clear() {
    this.job = null;
    this.sequence += 1;
    this.root.hidden = true;
    this.operations.hidden = true;
    this.operations.replaceChildren();
  }
}
