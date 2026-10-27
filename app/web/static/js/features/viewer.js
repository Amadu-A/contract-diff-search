// app/web/static/js/features/viewer.js
/** Один ленивый просмотрщик: страницы, масштаб, поворот, поиск и точные области. */
import { apiRequest, jobUrl } from '../api.js';
import { boundedPage, visibleDifference } from '../state.js';

export class Viewer {
  /** Подключает реальные действия к панели одной стороны; хранит только текущую страницу. */
  constructor(root, onNavigate, onError) {
    this.root = root;
    this.side = root.dataset.viewer;
    this.onNavigate = onNavigate;
    this.onError = onError;
    this.page = 1;
    this.zoom = 1;
    this.rotation = 0;
    this.sequence = 0;
    this.selected = null;
    this.regions = [];
    this.showUnverified = true;
    this.exports = [];
    this.searchItems = [];
    this.searchIndex = 0;
    this.paper = root.querySelector('[data-paper]');
    this.image = root.querySelector('[data-page-image]');
    root.querySelector('[data-page-prev]').addEventListener('click', () => this.go(this.page - 1, true));
    root.querySelector('[data-page-next]').addEventListener('click', () => this.go(this.page + 1, true));
    root.querySelector('[data-page-number]').addEventListener('change', event => this.go(event.target.value, true));
    root.querySelector('[data-zoom]').addEventListener('change', event => this.setZoom(Number(event.target.value)));
    root.querySelector('[data-zoom-in]').addEventListener('click', () => this.setZoom(Math.min(2, this.zoom + .25)));
    root.querySelector('[data-zoom-out]').addEventListener('click', () => this.setZoom(Math.max(.5, this.zoom - .25)));
    root.querySelector('[data-rotate]').addEventListener('click', () => {
      this.rotation = (this.rotation + 90) % 360;
      this.paper.style.transform = `rotate(${this.rotation}deg)`;
    });
    root.querySelector('[data-search-toggle]').addEventListener('click', () => {
      const form = root.querySelector('[data-search-form]');
      form.hidden = !form.hidden;
      if (!form.hidden) root.querySelector('[data-search-input]').focus();
    });
    root.querySelector('[data-search-form]').addEventListener('submit', event => {
      event.preventDefault();
      this.search().catch(onError);
    });
    root.querySelector('[data-search-next]').addEventListener('click', () => this.nextSearch());
    this.image.addEventListener('error', () => { if (this.job) this.onError(new Error('Не удалось загрузить страницу')); });
    root.querySelector('[data-export-word]').addEventListener('click', () => {
      if (this.wordFormat) window.location.assign(jobUrl(this.job, `/export/${this.side}?format=${this.wordFormat}&show_unverified=${this.showUnverified}`));
    });
  }

  /** Загружает метаданные завершённой редакции и проверенные ссылки экспорта. */
  async setDocument(job, document, exports) {
    this.job = job;
    this.document = document;
    this.root.querySelector('[data-viewer-name]').textContent = document.name;
    this.root.querySelector('[data-page-count]').textContent = `/ ${document.pages.length}`;
    this.root.querySelector('[data-page-number]').max = document.pages.length;
    for (const control of this.root.querySelectorAll('[data-rotate],[data-page-prev],[data-page-next],[data-page-number],[data-zoom],[data-zoom-in],[data-zoom-out],[data-search-toggle],[data-search-input],[data-search-form] button')) control.disabled = false;
    this.exports = exports;
    this.updateExports();
    this.root.querySelector('[data-viewer-empty]').hidden = true;
    this.paper.hidden = false;
    await this.go(1);
  }

  /** Выбирает артефакт по явному параметру видимости, не меняя результат сравнения. */
  updateExports() {
    if (!this.job) return;
    const pdf = this.root.querySelector('[data-export-pdf]');
    const artifact = this.exports.find(item => item.format === 'pdf' && (item.show_unverified ?? true) === this.showUnverified);
    if (artifact?.available) {
      pdf.href = jobUrl(this.job, `/export/${this.side}?format=pdf&show_unverified=${this.showUnverified}`);
      pdf.removeAttribute('aria-disabled');
    } else {
      pdf.removeAttribute('href');
      pdf.setAttribute('aria-disabled','true');
    }
    pdf.title = artifact?.reason || '';
    pdf.textContent = this.showUnverified ? '⇩ PDF с изменениями и непроверенными областями' : '⇩ PDF только с изменениями';
    const word = this.exports.find(item => item.format !== 'pdf' && (item.show_unverified ?? true) === this.showUnverified);
    const button = this.root.querySelector('[data-export-word]');
    button.hidden = !word;
    button.disabled = !word?.available;
    button.title = word?.reason || 'Экспорт прошёл проверку содержания и вёрстки';
    button.textContent = `▤ Скачать ${(word?.format || 'Word').toUpperCase()}`;
    this.wordFormat = word?.available ? word.format : null;
  }

  /** Переключает слои и ссылки без повторного рендера, OCR и сравнения. */
  setVisibility(value) {
    this.showUnverified = value;
    this.drawRegions();
    this.updateExports();
  }

  /** Обновляет метки текущей страницы после checkpoint ИИ, сохраняя положение прокрутки. */
  async refreshRegions() {
    if (!this.job) return;
    const sequence = this.sequence;
    const result = await apiRequest(jobUrl(this.job, `/regions/${this.side}/${this.page}`));
    if (sequence !== this.sequence) return;
    this.regions = result.items;
    this.drawRegions();
  }

  /** Меняет страницу и получает только её растр и области; старые ответы отбрасывает. */
  async go(value, notify = false) {
    if (!this.job) return;
    const sequence = ++this.sequence;
    this.page = boundedPage(value, this.document.pages.length);
    this.root.querySelector('[data-page-number]').value = this.page;
    this.root.querySelector('[data-page-prev]').disabled = this.page <= 1;
    this.root.querySelector('[data-page-next]').disabled = this.page >= this.document.pages.length;
    this.image.alt = `${this.document.name}, страница ${this.page}`;
    this.image.src = jobUrl(this.job, `/pages/${this.side}/${this.page}?scale=${Math.min(2.5, this.zoom * 1.5)}`);
    const page = this.document.pages[this.page - 1];
    this.paper.style.aspectRatio = `${page.width} / ${page.height}`;
    this.root.querySelector('[data-viewport]').scrollTop = 0;
    try {
      const result = await apiRequest(jobUrl(this.job, `/regions/${this.side}/${this.page}`));
      if (sequence !== this.sequence) return;
      this.regions = result.items;
      this.drawRegions();
      if (notify) this.onNavigate(this.side, this.page);
    } catch (error) { this.onError(error); }
  }

  /** Масштабирует координатный слой вместе с изображением без смещения подсветки. */
  setZoom(zoom) {
    this.zoom = zoom;
    this.paper.style.width = `${zoom * 100}%`;
    this.root.querySelector('[data-zoom]').value = String(zoom);
    if (this.job) this.image.src = jobUrl(this.job, `/pages/${this.side}/${this.page}?scale=${Math.min(2.5, zoom * 1.5)}`);
  }

  /** Рисует реальные прямоугольники; текст документов через HTML не вставляется. */
  drawRegions() {
    if (!this.document) return;
    const layer = this.root.querySelector('[data-regions]');
    layer.replaceChildren();
    const page = this.document.pages[this.page - 1];
    const entries = [...this.regions];
    for (const item of this.searchItems[this.searchIndex]?.regions || []) {
      if (item.page === this.page) entries.push({region: item, category:'search', id:'search', status:'confirmed'});
    }
    for (const item of entries) {
      if (!visibleDifference(item, this.showUnverified)) continue;
      const r = item.region;
      const region = document.createElement('span');
      region.className = 'viewer__region';
      region.dataset.differenceId = item.id;
      if (item.source === 'ai') region.classList.add('viewer__region--ai');
      if (item.source === 'ai' && item.status !== 'confirmed') region.classList.add('viewer__region--review');
      if (item.category.endsWith('_added')) region.classList.add('viewer__region--added');
      if (['unverified_region','ambiguous_match'].includes(item.category) || item.status === 'unverified') region.classList.add('viewer__region--warning');
      if (item.id === this.selected) region.classList.add('viewer__region--selected');
      if (item.category === 'search') region.classList.add('viewer__region--search');
      region.style.left = `${r.x0 / page.width * 100}%`;
      region.style.top = `${r.y0 / page.height * 100}%`;
      region.style.width = `${(r.x1 - r.x0) / page.width * 100}%`;
      region.style.height = `${(r.y1 - r.y0) / page.height * 100}%`;
      layer.append(region);
    }
  }

  /** Переходит именно к стороне выбранного изменения, включая разные номера страниц. */
  async select(difference) {
    this.selected = difference.id;
    const areas = difference[`${this.side}_regions`];
    if (!areas.length) {
      this.drawRegions();
      return;
    }
    await this.go(areas[0].page);
    if (this.selected !== difference.id) return;
    await this.image.decode().catch(() => {});
    if (this.selected !== difference.id) return;
    await new Promise(resolve => requestAnimationFrame(resolve));
    this.drawRegions();
    const viewport = this.root.querySelector('[data-viewport]');
    const page = this.document.pages[this.page - 1];
    viewport.scrollTop = Math.max(0, this.paper.clientHeight * areas[0].y0 / page.height - viewport.clientHeight / 3);
  }

  /** Ищет по серверному извлечению, включая OCR, и локализует первое совпадение. */
  async search() {
    const query = this.root.querySelector('[data-search-input]').value.trim();
    if (!query || !this.job) return;
    const response = await apiRequest(jobUrl(this.job, `/search/${this.side}?q=${encodeURIComponent(query)}`));
    this.searchItems = response.items;
    this.searchIndex = -1;
    this.nextSearch();
  }

  /** Переключает найденные совпадения, не создавая координат для непривязанных слов. */
  nextSearch() {
    if (!this.searchItems.length) {
      this.root.querySelector('[data-search-count]').textContent = 'Не найдено';
      return;
    }
    this.searchIndex = (this.searchIndex + 1) % this.searchItems.length;
    this.root.querySelector('[data-search-count]').textContent = `${this.searchIndex + 1}/${this.searchItems.length}`;
    const region = this.searchItems[this.searchIndex].regions[0];
    if (region) this.go(region.page).catch(this.onError);
  }

  /** Удаляет ссылки, изображения и состояние после сброса или истечения TTL. */
  clear() {
    this.sequence += 1;
    this.job = null;
    this.document = null;
    this.image.removeAttribute('src');
    this.paper.hidden = true;
    this.root.querySelector('[data-viewer-empty]').hidden = false;
    this.root.querySelector('[data-viewer-name]').textContent = 'Документ не загружен';
    this.root.querySelector('[data-page-count]').textContent = '/ —';
    for (const control of this.root.querySelectorAll('button,select,input')) control.disabled = true;
    this.root.querySelector('[data-export-pdf]').removeAttribute('href');
    this.root.querySelector('[data-export-pdf]').setAttribute('aria-disabled', 'true');
    this.root.querySelector('[data-export-word]').hidden = false;
    this.root.querySelector('[data-search-form]').hidden = true;
    this.searchItems = [];
    this.regions = [];
    this.selected = null;
    this.rotation = 0;
    this.paper.style.transform = '';
  }
}
