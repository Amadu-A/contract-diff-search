// app/web/static/js/features/upload.js
/** Парная загрузка: реальные метаданные, drag-and-drop, замена и ограничения. */
import { fileError } from '../state.js';

export class UploadCard {
  /** Связывает одну карточку с обработчиком изменения валидности формы. */
  constructor(root, maxMb, onChange) {
    this.root = root;
    this.maxMb = maxMb;
    this.onChange = onChange;
    this.input = root.querySelector('[data-file-input]');
    this.file = null;
    this.input.addEventListener('change', () => this.setFile(this.input.files[0]));
    root.querySelector('[data-remove-file]').addEventListener('click', () => this.setFile(null));
    root.addEventListener('dragover', event => {
      event.preventDefault();
      if (!this.input.disabled) root.classList.add('upload--drag');
    });
    root.addEventListener('dragleave', () => root.classList.remove('upload--drag'));
    root.addEventListener('drop', event => {
      event.preventDefault();
      root.classList.remove('upload--drag');
      if (!this.input.disabled) this.setFile(event.dataTransfer.files[0]);
    });
  }

  /** Проверяет выбранный файл; сервер затем независимо проверяет фактический формат. */
  setFile(file, notify = true) {
    const error = file ? fileError(file, this.maxMb) : '';
    this.file = error ? null : file || null;
    this.root.dataset.format = this.file?.name.split('.').pop().toLowerCase() || '';
    if (!file || error) this.input.value = '';
    const message = this.root.querySelector('[data-file-error]');
    message.textContent = error;
    message.hidden = !error;
    this.root.querySelector('[data-file-name]').textContent = this.file?.name || 'Файл не выбран';
    const metadata = this.file ? `${this.file.name.split('.').pop().toUpperCase()} · ${(this.file.size / 1024).toLocaleString('ru-RU', {maximumFractionDigits:0})} КБ · ${new Date(this.file.lastModified).toLocaleString('ru-RU')}` : `DOC · DOCX · PDF, до ${this.maxMb} МБ`;
    this.root.querySelector('[data-file-meta]').textContent = metadata;
    this.root.querySelector('[data-remove-file]').hidden = !this.file;
    if (notify) this.onChange();
  }

  /** Блокирует выбор/удаление на время отправки и активного анализа. */
  setBusy(busy) {
    this.input.disabled = busy;
    this.root.querySelector('[data-remove-file]').disabled = busy;
  }
}
