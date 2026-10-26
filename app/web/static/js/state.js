// app/web/static/js/state.js
/** Чистые правила валидации файлов и переходов состояния для UI и unit-тестов. */
export function fileError(file, maxMb) {
  if (!file) return 'Выберите файл';
  if (!/\.(doc|docx|pdf)$/i.test(file.name)) return 'Поддерживаются DOC, DOCX и PDF';
  if (file.size <= 0) return 'Файл пуст';
  if (file.size > maxMb * 1024 * 1024) return `Файл больше ${maxMb} МБ`;
  return '';
}

/** Конечное состояние останавливает polling, включая ошибки и отмену. */
export function terminalStatus(status) {
  return ['completed', 'completed_with_warnings', 'failed', 'cancelled', 'expired'].includes(status);
}

/** Ограничивает страницу реальным диапазоном документа. */
export function boundedPage(value, total) {
  const parsed = Number.parseInt(value, 10);
  return Math.min(Math.max(Number.isFinite(parsed) ? parsed : 1, 1), Math.max(1, total));
}

/** Объясняет категорию в списке без изменения машинного контракта API. */
export function categoryLabel(category) {
  return ({number_modified:'Число',text_modified:'Текст',text_added:'Добавлено',text_deleted:'Удалено',
    text_moved:'Перемещено',numbering_changed:'Нумерация',formatting_changed:'Оформление',
    table_modified:'Таблица',table_structure_changed:'Структура таблицы',structure_changed:'Структура',
    graphic_added:'Графика',graphic_deleted:'Графика',graphic_modified:'Графика',graphic_moved:'Графика',
    ambiguous_match:'Неоднозначно',unverified_region:'Не проверено'})[category] || category;
}
