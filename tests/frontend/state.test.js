// tests/frontend/state.test.js
/** Независимые проверки правил формы и границ навигации без браузерного DOM. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { fileError, boundedPage, terminalStatus, categoryLabel, isUnverified, visibleDifference, defaultCategories, differenceFilters } from '../../app/web/static/js/state.js';

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
test('скрытие предупреждений сохраняет подтверждённый факт с тем же якорем и не меняет статус', () => {
  const warning = {id:'warning',old_element:'same',status:'unverified',category:'text_modified'};
  const fact = {id:'fact',old_element:'same',status:'confirmed',category:'number_modified'};
  const items = [warning, fact, {id:'archived',active:false,status:'confirmed'}];
  assert.deepEqual(items.filter(item => visibleDifference(item,false)),[fact]);
  assert.equal(warning.status,'unverified');
  assert.deepEqual(items.filter(item => visibleDifference(item,true)),[warning,fact]);
});
test('все активные неподтверждённые события имеют единое правило видимости', () => {
  for (const item of [
    {category:'ambiguous_match',status:'ambiguous'},
    {category:'ambiguous_match',status:'confirmed'},
    {category:'unverified_region',status:'confirmed'},
    {category:'number_modified',status:'unverified'},
    {category:'text_modified',status:'needs_review'},
  ]) {
    assert.equal(isUnverified(item),true);
    assert.equal(visibleDifference(item,false),false);
    assert.equal(isUnverified({...item,active:false}),false);
  }
  assert.equal(isUnverified({category:'number_modified',status:'confirmed'}),false);
});

test('несколько категорий одинаково передаются списку и отчёту', () => {
  assert.deepEqual(defaultCategories, ['number_modified','text_modified','text_added','text_deleted','table_modified']);
  const query = differenceFilters(['text_modified','number_modified'], 'confirmed', false);
  assert.deepEqual(query.getAll('category'), ['text_modified','number_modified']);
  assert.equal(query.get('show_unverified'), 'false');
  assert.equal(query.get('status'), 'confirmed');
  assert.deepEqual(differenceFilters([], '', true).getAll('category'), []);
});
