const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const template = fs.readFileSync(path.join(__dirname, '../templates/orders.html'), 'utf8');
const source = template.slice(template.lastIndexOf('<script>') + 8, template.lastIndexOf('// Init'));

function runtime() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', innerHTML: '', value: '', dataset: {},
      classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {},
      querySelector() { return null; }
    });
    return elements.get(id);
  };
  let timeout;
  const context = vm.createContext({
    document: { getElementById: element, querySelector: element, querySelectorAll: () => [] },
    AbortController, URLSearchParams, Date,
    setTimeout: callback => { timeout = callback; return 1; }, clearTimeout() {},
    fetch: (_url, { signal }) => new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    })
  });
  vm.runInContext(source, context);
  return { context, element, expire: () => { assert.ok(timeout, 'A stalled request must have a deadline'); timeout(); } };
}

test('stalled order requests leave loading state and show a timeout message', async () => {
  const { context, element, expire } = runtime();
  const pending = vm.runInContext('loadOrderPage(1)', context);
  expire();
  await pending;
  assert.equal(vm.runInContext('listBusy', context), false);
  assert.match(element('listStatus').textContent, /逾時/);
});

test('stalled calendar requests also settle instead of waiting forever', async () => {
  const { context, expire } = runtime();
  const pending = vm.runInContext('loadDateSummary(true)', context);
  expire();
  await pending;
  assert.equal(vm.runInContext('dateSummaries', context), null);
});

test('cancelling an old request does not clear the newer request loading state', async () => {
  const { context, expire } = runtime();
  const first = vm.runInContext('loadOrderPage(1)', context);
  const second = vm.runInContext('loadOrderPage(2)', context);
  await first;
  assert.equal(vm.runInContext('listBusy', context), true);
  expire();
  await second;
  assert.equal(vm.runInContext('listBusy', context), false);
});
