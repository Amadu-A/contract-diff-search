// app/web/static/js/features/results.js
/** Фильтры, реальные счётчики, список и детали; недоверенные строки выводятся textContent. */
import { apiRequest, jobUrl } from '../api.js';
import { categoryLabel } from '../state.js';

export class Results {
  /** Подключает список к двум просмотрщикам и обработчику ошибок. */
  constructor(viewers, onError) {
    this.viewers = viewers;
    this.onError = onError;
    this.items = [];
    this.index = -1;
    this.generation = 0;
    this.showUnverified = true;
    document.querySelector('[data-filter-toggle]').addEventListener('click', () => {
      const filters = document.querySelector('[data-filters]');
      filters.hidden = !filters.hidden;
    });
    for (const filter of document.querySelectorAll('[data-category-filter],[data-status-filter]')) filter.addEventListener('change', () => this.load().catch(onError));
    document.querySelector('[data-load-more]').addEventListener('click', () => this.load(true).catch(onError));
    document.querySelector('[data-diff-prev]').addEventListener('click', () => this.select(this.index - 1));
    document.querySelector('[data-diff-next]').addEventListener('click', () => this.select(this.index + 1));
    document.querySelector('[data-go-region]').addEventListener('click', () => this.select(this.index));
    document.querySelector('[data-show-unverified]').addEventListener('click', () => {
      document.querySelector('[data-show-unverified-toggle]').checked = true;
      this.setVisibility(true);
      document.querySelector('[data-status-filter]').value = 'unverified';
      document.querySelector('[data-category-filter]').value = '';
      document.querySelector('[data-filters]').hidden = false;
      this.load().catch(onError);
    });
    document.querySelector('[data-dismiss-warning]').addEventListener('click', () => { document.querySelector('[data-warning]').hidden = true; });
  }

  /** Привязывает завершённое задание и его сводку к интерфейсу. */
  async setResult(job, summary, quiet = false) {
    this.job = job;
    for (const [key, value] of Object.entries(summary.statistics)) {
      const node = document.querySelector(`[data-stat="${key}"]`);
      if (node) node.textContent = value;
    }
    const counts = summary.statistics;
    document.querySelector('[data-confirmed-breakdown]').textContent = `Алгоритмы: ${counts.algorithms_confirmed || 0}; ИИ: ${counts.ai_confirmed || 0}; совместно: ${counts.both_confirmed || 0}`;
    const warning = summary.statistics.unverified > 0 || summary.warnings.length > 0;
    document.querySelector('[data-warning]').hidden = !warning;
    document.querySelector('[data-warning-text]').textContent = summary.warnings.join('. ') || 'Некоторые участки не удалось проверить автоматически. Просмотрите их вручную.';
    await this.load(false, quiet);
  }

  /** Скрывает только предупреждения и синхронизирует обе панели и скачивание. */
  async setVisibility(value) {
    this.showUnverified = value;
    this.viewers.forEach(viewer => viewer.setVisibility(value));
    if (!value) {
      if (document.querySelector('[data-status-filter]').value === 'unverified') document.querySelector('[data-status-filter]').value = '';
      if (['unverified_region','ambiguous_match'].includes(document.querySelector('[data-category-filter]').value)) document.querySelector('[data-category-filter]').value = '';
    }
    if (this.job) await this.load();
  }

  /** Загружает порцию отфильтрованных записей; сброс отбрасывает запоздалый ответ. */
  async load(append = false, quiet = false) {
    if (!this.job) return;
    const generation = ++this.generation;
    const category = document.querySelector('[data-category-filter]').value;
    const status = document.querySelector('[data-status-filter]').value;
    const offset = append ? this.items.length : 0;
    const selected = this.items[this.index]?.id;
    const list = document.querySelector('[data-differences]');
    const scrollTop = list.scrollTop;
    const response = await apiRequest(jobUrl(this.job, `/differences?category=${encodeURIComponent(category)}&status=${encodeURIComponent(status)}&offset=${offset}&limit=200&show_unverified=${this.showUnverified}`));
    if (generation !== this.generation) return;
    this.items = append ? [...this.items, ...response.items] : response.items;
    document.querySelector('[data-list-count]').textContent = response.total;
    document.querySelector('[data-load-more]').hidden = this.items.length >= response.total;
    this.renderList();
    if (quiet) list.scrollTop = scrollTop;
    const preserved = quiet ? this.items.findIndex(item => item.id === selected) : -1;
    if (this.items.length) await this.select(preserved >= 0 ? preserved : append ? Math.max(0, this.index) : 0, preserved < 0);
    else this.clearDetails('По выбранным фильтрам различий нет');
  }

  /** Создаёт доступные кнопки без вставки имён файлов или текста через innerHTML. */
  renderList() {
    const list = document.querySelector('[data-differences]');
    list.replaceChildren();
    this.items.forEach((item, index) => {
      const li = document.createElement('li');
      li.className = 'differences__item';
      const button = document.createElement('button');
      button.className = 'differences__button';
      button.type = 'button';
      button.dataset.index = index;
      const number = document.createElement('span');
      number.className = 'differences__number';
      number.textContent = index + 1;
      const summary = document.createElement('span');
      summary.className = 'differences__summary';
      const title = document.createElement('strong');
      title.textContent = item.title;
      const meta = document.createElement('small');
      const page = item.new_regions[0]?.page || item.old_regions[0]?.page;
      meta.textContent = `${page ? `Стр. ${page} · ` : ''}${categoryLabel(item.category)}${item.status === 'unverified' ? ' · Не проверено' : ''}`;
      summary.append(title, meta);
      button.append(number, summary);
      button.addEventListener('click', () => this.select(index));
      li.append(button);
      list.append(li);
    });
  }

  /** Показывает выбранное событие и соответствующие области обеих редакций. */
  async select(index, navigate = true) {
    if (index < 0 || index >= this.items.length) return;
    this.index = index;
    const item = this.items[index];
    document.querySelector('[data-detail-empty]').hidden = true;
    document.querySelector('[data-detail-body]').hidden = false;
    document.querySelector('[data-detail-title]').textContent = item.title;
    document.querySelector('[data-detail-position]').textContent = `${index + 1}/${this.items.length}`;
    document.querySelector('[data-detail-meta]').textContent = `Наша: ${item.old_regions[0]?.page || 'нет привязки'} · Контрагент: ${item.new_regions[0]?.page || 'нет привязки'}`;
    document.querySelector('[data-detail-category]').textContent = categoryLabel(item.category);
    const causes = {unreadable_image:'Изображение не удалось прочитать',mapping_not_found:'Соответствие фрагментов не установлено',partial_coverage:'Проверена только часть области',unsupported_property:'Свойство невозможно проверить по изображению',model_error:'Ошибка запроса к модели',conflicting_evidence:'Противоречивые результаты проверки',missing_geometry:'Нет надёжной геометрической привязки'};
    document.querySelector('[data-detail-reason]').textContent = (item.source === 'ai' ? `${item.status === 'confirmed' ? 'Подтверждено ИИ по исходным документам' : 'Обнаружено ИИ, требует проверки'}. ` : '') + item.reason + (item.unverified_reason ? `. ${causes[item.unverified_reason] || item.unverified_reason}` : '');
    document.querySelector('[data-detail-old]').textContent = item.old_text || 'Фрагмент отсутствует';
    document.querySelector('[data-detail-new]').textContent = item.new_text || 'Фрагмент отсутствует';
    for (const side of ['old','new']) {
      const image = document.querySelector(`[data-detail-${side}-image]`);
      image.hidden = !item.category.startsWith('graphic_') || !item[`${side}_regions`].length;
      if (!image.hidden) image.src = jobUrl(this.job, `/crop/${side}/${encodeURIComponent(item.id)}`);
      else image.removeAttribute('src');
    }
    document.querySelector('[data-detail-source]').textContent = item.source === 'ai' ? `Источник: ИИ — ${item.verified_by === 'ai_visual' ? 'визуальная проверка исходных документов' : item.status === 'confirmed' ? 'проверка с независимым извлечением' : 'неподтверждённое замечание'}` : item.verified_by ? 'Источник: алгоритмы; перепроверено ИИ' : 'Источник: детерминированное сравнение';
    document.querySelector('[data-diff-prev]').disabled = index === 0;
    document.querySelector('[data-diff-next]').disabled = index === this.items.length - 1;
    for (const button of document.querySelectorAll('[data-index]')) button.setAttribute('aria-current', String(Number(button.dataset.index) === index));
    if (navigate) await Promise.all(this.viewers.map(viewer => viewer.select(item))).catch(this.onError);
  }

  /** Убирает устаревшие детали при фильтрации или удалении задания. */
  clearDetails(message = 'Выберите изменение в списке') {
    document.querySelector('[data-detail-empty]').textContent = message;
    document.querySelector('[data-detail-empty]').hidden = false;
    document.querySelector('[data-detail-body]').hidden = true;
    document.querySelector('[data-detail-position]').textContent = '—';
    document.querySelector('[data-diff-prev]').disabled = true;
    document.querySelector('[data-diff-next]').disabled = true;
  }

  /** Очищает все данные результата, не сохраняя историю в браузере. */
  clear() {
    this.generation += 1;
    this.job = null;
    this.items = [];
    this.index = -1;
    document.querySelector('[data-category-filter]').value = '';
    document.querySelector('[data-status-filter]').value = '';
    document.querySelector('[data-differences]').replaceChildren();
    document.querySelector('[data-list-count]').textContent = '0';
    document.querySelector('[data-load-more]').hidden = true;
    document.querySelector('[data-warning]').hidden = true;
    for (const node of document.querySelectorAll('[data-stat]')) node.textContent = '—';
    document.querySelector('[data-confirmed-breakdown]').textContent = 'уникальные подтверждённые изменения';
    this.clearDetails('Различия появятся после анализа документов');
  }
}
