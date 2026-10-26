// tests/frontend/state.test.js
/** Независимые проверки правил формы и границ навигации без браузерного DOM. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { fileError, boundedPage, terminalStatus, categoryLabel } from '../../app/web/static/js/state.js';

test('валидация принимает все три формата и граничный размер', () => {
  for (const name of ['a.doc','b.DOCX','c.pdf']) assert.equal(fileError({name,size:1024**2},1),'');
  assert.match(fileError({name:'a.pdf',size:1024**2+1},1),/больше/);
  assert.match(fileError({name:'x.exe',size:1},100),/DOC/);
  assert.match(fileError({name:'a.pdf',size:0},1),/пуст/);
});
test('страница ограничивается реальным диапазоном', () => {
  assert.equal(boundedPage(-1,7),1);
  assert.equal(boundedPage(800,7),7);
  assert.equal(boundedPage('ошибка',7),1);
});
test('все конечные статусы останавливают polling', () => {
  for (const status of ['completed','completed_with_warnings','failed','cancelled','expired']) assert.equal(terminalStatus(status),true);
  assert.equal(terminalStatus('processing'),false);
  assert.equal(categoryLabel('number_modified'),'Число');
});
