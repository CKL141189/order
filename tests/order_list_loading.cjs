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
  const timers = new Map();
  let timerId = 0;
  const context = vm.createContext({
    document: { getElementById: element, querySelector: element, querySelectorAll: () => [] },
    AbortController, URLSearchParams, Date,
    setTimeout: (callback, delay) => { timers.set(++timerId, {callback, delay}); return timerId; },
    clearTimeout: id => timers.delete(id),
    fetch: (_url, { signal }) => new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    })
  });
  vm.runInContext(source.replace('{{ date_tabs[1].date_key }}', '11/9'), context);
  return { context, element, expire: () => {
    const timeout = [...timers.values()].filter(t => t.delay === 25000).at(-1);
    assert.ok(timeout, 'A stalled request must have a deadline'); timeout.callback();
  } };
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

test('today becomes interactive before calendar or neighboring requests start', async () => {
  const { context, element } = runtime();
  const urls = [];
  let finishOrders;
  context.fetch = url => {
    urls.push(url);
    return new Promise(resolve => { if (!url.includes('summary')) finishOrders = resolve; });
  };
  vm.runInContext(template.slice(template.lastIndexOf('// Init'), template.lastIndexOf('</script>')), context);
  assert.equal(urls.length, 1);
  assert.ok(urls[0].startsWith('/api/orders?'));
  finishOrders({ok:true, json:async()=>({ok:true,html:'today',page:1,pages:1,total:1,counts:{}})});
  await new Promise(setImmediate);
  assert.equal(element('orderGrid').innerHTML, 'today');
  assert.equal(element('orderGrid').inert, false);
  assert.ok(urls[1].startsWith('/api/orders/summary?'));
});

test('neighbor dates expand outward, including across year boundaries', () => {
  const { context } = runtime();
  const result = vm.runInContext("orderedPreloadDates([1113,1107,1110,1108,1112,1106,1109,1111].map(date=>({date,total:1})), '11/9')", context);
  assert.deepEqual(Array.from(result), ['11/8','11/10','11/7','11/11','11/6','11/12','11/13']);
  assert.equal(vm.runInContext("orderedPreloadDates([{date:101,total:1},{date:1229,total:1}], '12/31')[0]", context), '1/1');
});

test('prefetched dates render without another blocking request', async () => {
  const { context, element } = runtime();
  context.fetch = () => { throw new Error('Unexpected network request'); };
  vm.runInContext(`currentTab='11/10'; dateSummaries=[];
    rememberOrderPage('tab=11%2F10&sort=date&page=1&driver=all&q=&invoice=0&luxury=0&svc=0',
      {ok:true,html:'prefetched',page:1,pages:1,total:1,counts:{}});`, context);
  await vm.runInContext('loadOrderPage(1)', context);
  assert.equal(element('orderGrid').innerHTML, 'prefetched');
  assert.equal(vm.runInContext('listBusy', context), false);
});

test('foreground requests cancel background work and discard its late response', async () => {
  const { context } = runtime();
  let finishBackground, backgroundSignal;
  vm.runInContext('dateSummaries=[{date:1110,total:1}];', context);
  context.fetch = (_url, {signal}) => {
    backgroundSignal = signal;
    return new Promise(resolve => { finishBackground = resolve; });
  };
  const background = vm.runInContext('prefetchNextDate()', context);
  assert.equal(vm.runInContext('listBusy', context), false);
  context.fetch = async () => ({ok:true,json:async()=>({ok:true,html:'foreground',page:1,pages:1,total:1,counts:{}})});
  await vm.runInContext('loadOrderPage(1)', context);
  assert.equal(backgroundSignal.aborted, true);
  finishBackground({ok:true,json:async()=>({ok:true,html:'old background',page:1,pages:1,total:1,counts:{}})});
  await background;
  assert.equal(vm.runInContext("[...orderPageCache.values()].some(v=>v.data.html==='old background')", context), false);
});

test('writes invalidate prefetched records immediately and suspend preloading', async () => {
  const { context } = runtime();
  let finishWrite;
  vm.runInContext("rememberOrderPage('old', {html:'old'});", context);
  context.fetch = () => new Promise(resolve => { finishWrite = resolve; });
  const write = vm.runInContext("postJson('/api/orders/1', {note:'new'})", context);
  assert.equal(vm.runInContext('orderPageCache.size', context), 0);
  assert.equal(vm.runInContext('pendingWrites', context), 1);
  finishWrite({ok:true,json:async()=>({ok:true})});
  await write;
  assert.equal(vm.runInContext('pendingWrites', context), 0);
});

test('prefetch cache has page, memory and expiry limits', () => {
  const { context } = runtime();
  vm.runInContext("for(let i=0;i<30;i++) rememberOrderPage('page'+i,{html:'small'});", context);
  assert.ok(vm.runInContext('orderPageCache.size <= 16', context));
  assert.equal(vm.runInContext("rememberOrderPage('too-large',{html:'x'.repeat(5*1024*1024)})", context), false);
  vm.runInContext("const key=orderPageCache.keys().next().value; orderPageCache.get(key).expiresAt=0;", context);
  assert.equal(vm.runInContext('cachedOrderPage(key)', context), null);
});

test('background work is bounded even when earlier cache entries expire', async () => {
  const { context } = runtime();
  let requests = 0;
  context.fetch = () => { requests++; throw new Error('Prefetch budget exceeded'); };
  vm.runInContext("dateSummaries=[{date:1110,total:1}]; for(let i=0;i<4;i++) preloadedPages.add('done'+i);", context);
  await vm.runInContext('prefetchNextDate()', context);
  assert.equal(requests, 0);
  assert.equal(vm.runInContext('backgroundController', context), null);
});
