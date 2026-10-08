// Execute the shipped service code with controlled browser file/clipboard APIs.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {dirname, join} from 'node:path';
import test from 'node:test';

const source = readFileSync(process.env.PYSUAL_DOCUMENT_HOST, 'utf8');
const inputModule = `data:text/javascript;base64,${Buffer.from(readFileSync(join(dirname(process.env.PYSUAL_DOCUMENT_HOST), 'input.js'), 'utf8')).toString('base64')}`;
const servicesModule = `data:text/javascript;base64,${Buffer.from(readFileSync(join(dirname(process.env.PYSUAL_DOCUMENT_HOST), 'services.js'), 'utf8')).toString('base64')}`;
const svgSource = readFileSync(join(dirname(process.env.PYSUAL_DOCUMENT_HOST), 'svg.js'), 'utf8')
  .replace('"./input.js"', JSON.stringify(inputModule)).replace('"./services.js"', JSON.stringify(servicesModule));
const {browserOperation, pointerEvent, viewportSnapshot} = await import(`data:text/javascript;base64,${Buffer.from(svgSource).toString('base64')}`);
const servicesSource = readFileSync(join(dirname(process.env.PYSUAL_DOCUMENT_HOST), 'services.js'), 'utf8');
const {createBrowserServices} = await import(`data:text/javascript;base64,${Buffer.from(servicesSource).toString('base64')}`);
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; }); return {promise, resolve, reject}; };

class Element extends EventTarget {
  constructor(tag) { super(); this.tag = tag; this.style = {}; this.children = []; this.textContent = ''; this.disabled = false; this.parent = null; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); this.parent = null; }
  close() {}
  showModal() {}
  focus() {}
  select() {}
}

function fixture(t, {picker, clipboard = {writeText: async () => { throw new Error('Denied'); }}} = {}) {
  const document = {body: new Element('body'), createElement: tag => new Element(tag)};
  const revoked = [];
  const url = {createObjectURL: () => 'blob:test', revokeObjectURL: value => revoked.push(value)};
  for (const [name, value] of Object.entries({document, window: {showSaveFilePicker: picker}, URL: url, navigator: {clipboard}})) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, {configurable: true, writable: true, value});
    t.after(() => previous ? Object.defineProperty(globalThis, name, previous) : delete globalThis[name]);
  }
  const browserServices = createBrowserServices();
  const bridge = {service: (command, signal) => browserOperation(browserServices, command, signal),
    getPanel: () => document.body.children.find(node => node.tag === 'dialog') || null};
  const all = () => { const nodes = []; const visit = node => { nodes.push(node); node.children.forEach(visit); }; visit(document.body); return nodes; };
  return {bridge, revoked, all, find: text => all().find(node => node.textContent === text), controller: new AbortController()};
}
const save = {method: 'save_text_file', text: 'retained contents', name: 'project.json'};

function inputFixture(t) {
  const events = [];
  const window = {devicePixelRatio: 2, visualViewport: {height: 800, offsetTop: 20, scale: 1}, getComputedStyle: () => ({})};
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', {configurable: true, writable: true, value: window});
  t.after(() => previous ? Object.defineProperty(globalThis, 'window', previous) : delete globalThis.window);
  const surface = {getBoundingClientRect: () => ({left: 10, top: 20, width: 600, height: 800})};
  let textActive = true;
  const bridge = {point: event => pointerEvent(surface, event),
    viewport: () => events.push({viewport: viewportSnapshot(surface, {}, textActive)}),
    setTextActive: value => {textActive = value;}};
  return {bridge, window, events};
}

test('extra pointer buttons keep their identity and cannot become primary clicks', t => {
  const f = inputFixture(t);
  for (const button of [0, 1, 2, 3, 4]) {
    const point = f.bridge.point({button, clientX: 40, clientY: 60, pointerId: 9,
      pointerType: 'mouse', shiftKey: true, metaKey: true});
    assert.deepEqual(point, {x: 30, y: 40, button: button + 1,
      pointer_id: 9, pointer_kind: 'mouse', shift: true, ctrl: true});
  }
});

test('pinch zoom is not keyboard coverage while real keyboard coverage is retained', t => {
  const f = inputFixture(t);
  const occlusion = () => { f.bridge.viewport(); return f.events.at(-1).viewport.keyboard_occlusion; };
  assert.equal(occlusion(), 0);
  f.window.visualViewport.height = 400;
  f.window.visualViewport.scale = 2;
  assert.equal(occlusion(), 0);
  f.window.visualViewport.scale = 1;
  assert.equal(occlusion(), 400);
  f.bridge.setTextActive(false);
  assert.equal(occlusion(), 0);
  f.bridge.setTextActive(true);
  f.window.visualViewport = null;
  assert.equal(occlusion(), 0);
});

test('native save acknowledges only after writable stream closes', async t => {
  const closed = deferred(), writes = [];
  const f = fixture(t, {picker: async () => ({name: 'chosen.json', createWritable: async () => ({write: async text => writes.push(text), close: () => closed.promise, abort: async () => {}})})});
  let settled = false;
  const result = f.bridge.service(save, f.controller.signal).then(value => { settled = true; return value; });
  const click = f.find('Choose destination').onclick();
  await tick();
  assert.deepEqual(writes, ['retained contents']);
  assert.equal(settled, false);
  closed.resolve(); await click;
  assert.equal(await result, 'chosen.json');
  assert.equal(f.bridge.getPanel(), null);
});

test('cancelled picker cannot later create a writable file', async t => {
  const picked = deferred(); let opened = 0;
  const f = fixture(t, {picker: () => picked.promise});
  const result = f.bridge.service(save, f.controller.signal);
  const click = f.find('Choose destination').onclick();
  f.find('Cancel').onclick();
  assert.equal(await result, null);
  picked.resolve({createWritable: async () => { opened++; }});
  await click;
  assert.equal(opened, 0);
});

test('Python cancellation while write is pending aborts without commit', async t => {
  const written = deferred(); let committed = 0, aborted = 0;
  const f = fixture(t, {picker: async () => ({createWritable: async () => ({write: () => written.promise, close: async () => { committed++; }, abort: async () => { aborted++; }})})});
  const result = f.bridge.service(save, f.controller.signal);
  const rejected = assert.rejects(result, /cancelled/);
  const click = f.find('Choose destination').onclick();
  await tick(); f.controller.abort();
  await rejected;
  written.resolve(); await click;
  assert.equal(committed, 0); assert.equal(aborted, 1);
});

test('download handoff requires saved confirmation and retains URL until dialog ends', async t => {
  const f = fixture(t); let settled = false;
  const result = f.bridge.service(save, f.controller.signal).then(value => { settled = true; return value; });
  const confirm = f.find('I saved the file');
  assert.equal(confirm.disabled, true);
  confirm.onclick(); await tick(); assert.equal(settled, false);
  f.find('Download project.json').onclick({preventDefault() {}});
  await tick(); assert.equal(settled, false); assert.deepEqual(f.revoked, []);
  assert.equal(confirm.disabled, false); confirm.onclick();
  assert.equal(await result, true); assert.deepEqual(f.revoked, ['blob:test']);
});

test('cancelled download reports no save and revokes its temporary URL', async t => {
  const f = fixture(t);
  const result = f.bridge.service(save, f.controller.signal);
  f.find('Download project.json').onclick({preventDefault() {}});
  f.find('Cancel').onclick();
  assert.equal(await result, null);
  assert.deepEqual(f.revoked, ['blob:test']);
});

test('clipboard rejection after cancellation cannot open an abandoned dialog', async t => {
  const writing = deferred();
  const f = fixture(t, {clipboard: {writeText: () => writing.promise}});
  const result = f.bridge.service({method: 'clipboard_write', text: 'copy'}, f.controller.signal);
  f.controller.abort(); writing.reject(new Error('Denied'));
  await assert.rejects(result, /cancelled/);
  assert.equal(f.bridge.getPanel(), null);
});

test('clipboard fallback completes only on a real copy action', async t => {
  const f = fixture(t); let settled = false;
  const result = f.bridge.service({method: 'clipboard_write', text: 'exact contents'}, f.controller.signal).then(value => { settled = true; return value; });
  await tick();
  assert.equal(f.find('Done'), undefined); assert.equal(settled, false);
  const field = f.all().find(node => node.tag === 'textarea');
  const copied = [], event = new Event('copy', {cancelable: true});
  Object.defineProperty(event, 'clipboardData', {value: {setData: (...args) => copied.push(args)}});
  field.dispatchEvent(event);
  assert.equal(await result, true);
  assert.deepEqual(copied, [['text/plain', 'exact contents']]);
  assert.equal(event.defaultPrevented, true);
});

test('native file picker cancellation completes import as cancelled', async t => {
  const f = fixture(t);
  const result = f.bridge.service({method: 'open_text_file'}, f.controller.signal);
  f.all().find(node => node.tag === 'input').oncancel();
  assert.equal(await result, null);
});

function transportFixture(statuses, queuedReplies) {
  const requests = [], messages = [];
  const sendSource = source.slice(source.indexOf('  async function send()'), source.indexOf('  function apply('));
  const factory = new Function('fetch', 'queueMicrotask', 'AbortSignal', 'TextEncoder', 'queuedReplies', 'messages',
    `let active=true,sending=false,events=[],replies=queuedReplies;
     const connection=new AbortController();
     const MAX_TEXT=8*1024*1024,MAX_REQUEST=MAX_TEXT*6+65536,encoder=new TextEncoder(),headers={};
     const announce=message=>messages.push(message),stop=message=>{active=false;announce(message);};
     ${sendSource};return {send, pending:()=>replies.length};`);
  const pending = [];
  const bridge = factory(async (url, options) => {
    requests.push(JSON.parse(options.body));
    const status = statuses.shift() || 200;
    return {status, ok: status === 200};
  }, callback => pending.push(callback), AbortSignal, TextEncoder, queuedReplies, messages);
  return {bridge, requests, messages, async drain() { await bridge.send(); while (pending.length) await pending.shift()(); }};
}
for (const status of [400, 413, 415]) test(`permanent HTTP ${status} completes a result with one small error instead of replaying its payload`, async () => {
  const f = transportFixture([status, 200], [{id: 7, value: {text: 'original payload'}}]);
  await f.drain();
  assert.equal(f.requests.length, 2);
  assert.equal(f.requests[0].replies[0].value.text, 'original payload');
  assert.match(f.requests[1].replies[0].error, new RegExp(String(status)));
  assert.equal(f.requests[1].replies[0].id, 7);
  assert.equal(f.bridge.pending(), 0);
});

test('legal maximally escaped text uses a bounded encoded envelope and a separate second reply', async () => {
  const text = '\0'.repeat(8 * 1024 * 1024);
  const f = transportFixture([200, 200], [{id: 8, value: {name: 'nul.txt', text}}, {id: 9, value: 'next'}]);
  await f.drain();
  assert.equal(f.requests.length, 2);
  assert.equal(f.requests[0].replies.length, 1);
  assert.equal(f.requests[0].replies[0].value.text, text);
  const encoded = new TextEncoder().encode(JSON.stringify(f.requests[0])).length;
  assert.ok(encoded > 8 * 1024 * 1024 + 65536);
  assert.ok(encoded <= 8 * 1024 * 1024 * 6 + 65536);
  assert.equal(f.requests[1].replies[0].id, 9);
});

test('invalid input token stops without retrying actions or pending replies', async () => {
  const f = transportFixture([403], [{id: 7, value: 'reply'}]);
  await f.drain();
  assert.equal(f.requests.length, 1); assert.equal(f.bridge.pending(), 0);
  assert.match(f.messages.at(-1), /no valid application connection/);
});

function frameFixture(fetch, {EventSource, clock = {setTimeout, clearTimeout, Date}} = {}) {
  const messages = [], packets = [];
  const frameSource = source.slice(source.indexOf('  function receiveFrame('), source.indexOf('  function disconnect()'));
  const factory = new Function('fetch', 'messages', 'packets', 'EventSource', 'setTimeout', 'clearTimeout', 'Date', `
    let active=true,revision=-1,connected=false,disconnectedAt=null,disconnectTimer=null;
    const token='test',headers={},connection=new AbortController(),announce=message=>messages.push(message);
    const stop=message=>{active=false;connection.abort();announce(message);};
    const apply=packet=>packets.push(packet);
    ${frameSource};return {frames,startFrames,stop,signal:connection.signal};`);
  return {bridge: factory(fetch, messages, packets, EventSource, clock.setTimeout, clock.clearTimeout, clock.Date), messages, packets};
}

test('an in-flight decoded frame cannot overwrite shutdown state or start another operation', async () => {
  const body = deferred();
  const f = frameFixture(async () => ({ok: true, json: () => body.promise}));
  const pending = f.bridge.frames(); await tick();
  f.bridge.stop('Closed'); body.resolve({commands: [{method: 'open_text_file', id: 8}]});
  await pending;
  assert.equal(f.bridge.signal.aborted, true); assert.deepEqual(f.packets, []);
  assert.deepEqual(f.messages, ['Closed']);
});

test('a cancelled frame request cannot change shutdown into a reconnect message', async () => {
  const request = deferred();
  const f = frameFixture(() => request.promise), pending = f.bridge.frames();
  f.bridge.stop('Closed'); request.reject(new Error('Request aborted'));
  await pending;
  assert.deepEqual(f.messages, ['Closed']); assert.deepEqual(f.packets, []);
});

test('invalid frame token closes the connection without a reconnect attempt', async () => {
  let requests = 0;
  const f = frameFixture(async () => { requests++; return {ok: false, status: 403}; });
  await f.bridge.frames();
  assert.equal(requests, 1); assert.equal(f.bridge.signal.aborted, true);
  assert.match(f.messages.at(-1), /no valid application connection/);
});

function streamFixture() {
  const timers = new Map(), streams = [];
  let now = 0, serial = 0;
  const clock = {
    setTimeout: (callback, delay) => { timers.set(++serial, {callback, at: now + delay}); return serial; },
    clearTimeout: id => timers.delete(id), Date: {now: () => now},
    advance(milliseconds) {
      now += milliseconds;
      for (const [id, timer] of timers) if (timer.at <= now) { timers.delete(id); timer.callback(); }
    },
  };
  class EventSource {
    static CLOSED = 2;
    constructor() { this.readyState = 1; streams.push(this); }
    close() { this.readyState = EventSource.CLOSED; }
    message(packet = {revision: 1, commands: []}) { this.onmessage({data: JSON.stringify(packet)}); }
    error() { this.readyState = 0; this.onerror(); }
  }
  const f = frameFixture(() => { throw new Error('Unexpected fetch fallback'); }, {EventSource, clock});
  f.bridge.startFrames();
  return {...f, stream: streams[0], clock, timers};
}

test('CONNECTING reports loss and reaches its deadline without another error', () => {
  const f = streamFixture();
  f.stream.message(); f.stream.error();
  assert.match(f.messages.at(-1), /disconnected.*Reconnecting/);
  f.clock.advance(7999); assert.equal(f.bridge.signal.aborted, false);
  f.clock.advance(1); assert.equal(f.bridge.signal.aborted, true);
  assert.equal(f.stream.readyState, 2);
  assert.match(f.messages.at(-1), /Python connection ended/);
});

test('successful delivery cancels loss timeout and later loss gets a fresh deadline', () => {
  const f = streamFixture();
  f.stream.message(); f.stream.error(); f.clock.advance(4000);
  f.stream.message({revision: 2, commands: []});
  assert.equal(f.messages.at(-1), ''); assert.equal(f.timers.size, 0);
  f.clock.advance(8000); assert.equal(f.bridge.signal.aborted, false);
  f.stream.error(); f.clock.advance(4000); f.stream.error();
  f.clock.advance(3999); assert.equal(f.bridge.signal.aborted, false);
  f.clock.advance(1); assert.equal(f.bridge.signal.aborted, true);
});

test('initial connection waits visibly and late errors cannot overwrite shutdown', () => {
  const f = streamFixture();
  f.stream.error(); assert.match(f.messages.at(-1), /Waiting for the Python application/);
  assert.equal(f.timers.size, 0);
  f.stream.message(); f.stream.error();
  f.bridge.stop('Closed'); f.clock.advance(8000); f.stream.error();
  assert.equal(f.messages.at(-1), 'Closed');
});

test('repeated metadata packets cannot execute an active or completed service twice', async () => {
  const answer = deferred(), calls = [];
  const applySource = source.slice(source.indexOf('  function apply('), source.indexOf('  function receiveFrame('));
  const operationSource = source.slice(source.indexOf('  async function runOperation('), source.indexOf('  function stop('));
  const bridge = new Function('service', `
    let revision=-1,replies=[];
    const operations=new Map(),complete=new Set(),client={apply(){},service},send=()=>{},stop=()=>{};
    ${applySource}${operationSource};return {apply};
  `)(command => { calls.push(command.id); return answer.promise; });
  const packet = {revision: 1, commands: [{id: 7, method: 'open_text_file'}]};
  bridge.apply(packet); bridge.apply(packet);
  assert.deepEqual(calls, [7]);
  answer.resolve(null); await tick(); bridge.apply(packet);
  assert.deepEqual(calls, [7]);
});

test('navigation and tab-close lifecycle events send only one keepalive disconnect', () => {
  const leaveSource = source.slice(source.indexOf('  function disconnect()'), source.indexOf('  document.getElementById("quit")'));
  for (const events of [['beforeunload', 'pagehide'], ['pagehide']]) {
    const window = new EventTarget(), requests = [];
    new Function('window', 'fetch', `
      let departing=false;
      const headers={'X-Pysual-Token':'test'},connection=new AbortController();
      ${leaveSource}
    `)(window, async (url, options) => requests.push({url, ...options}));
    for (const event of events) window.dispatchEvent(new Event(event));
    assert.equal(requests.length, 1);
    assert.equal(requests[0].keepalive, true);
    assert.deepEqual(JSON.parse(requests[0].body), {disconnect: true, events: [{kind: 'blur'}]});
  }
});
