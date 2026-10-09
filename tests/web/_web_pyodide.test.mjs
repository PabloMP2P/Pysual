// Exercise the shipped host module with controlled SVG DOM/file primitives.
// These are bridge contracts, not a substitute for a real browser acceptance run.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {dirname, join} from 'node:path';
import test from 'node:test';

const inputModule = `data:text/javascript;base64,${Buffer.from(readFileSync(join(dirname(process.env.PYSUAL_TEST_HOST), 'input.js'), 'utf8')).toString('base64')}`;
const servicesModule = `data:text/javascript;base64,${Buffer.from(readFileSync(join(dirname(process.env.PYSUAL_TEST_HOST), 'services.js'), 'utf8')).toString('base64')}`;
const svgSource = readFileSync(join(dirname(process.env.PYSUAL_TEST_HOST), 'svg.js'), 'utf8')
  .replace('"./input.js"', JSON.stringify(inputModule))
  .replace('"./services.js"', JSON.stringify(servicesModule));
const svgModule = `data:text/javascript;base64,${Buffer.from(svgSource).toString('base64')}`;
const source = readFileSync(process.env.PYSUAL_TEST_HOST, 'utf8')
  .replace('"./svg.js"', JSON.stringify(svgModule));
const {createHost} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const tick = () => new Promise(resolve => setImmediate(resolve));

const scene = (nodes, extra = {}) => ({revision: 1, reset: true, length: nodes.length,
  updates: nodes.map((node, index) => [index, node]), width: 640, height: 400,
  title: 'SVG fixture', text_input: null, ...extra});
const gradient = (id, color = '#123') => ({tag: 'linearGradient', attrs: {id}, children: [
  {tag: 'stop', attrs: {offset: '0', 'stop-color': color}},
  {tag: 'stop', attrs: {offset: '1', 'stop-color': '#fff'}},
]});

test('gradient deltas preserve untouched resources and keep body positions independent', t => {
  const f = fixture(t), definition = gradient('face');
  const rect = {tag: 'rect', attrs: {fill: 'url(#face)', width: '20'}};
  f.host.present(JSON.stringify(scene([rect], {definitions: [definition]})));
  const bank = f.surface.children.find(node => node.localName === 'defs');
  const resource = bank.children[0], stop = resource.children[0];
  const body = f.surface.children.find(node => node.localName === 'rect');
  const writes = stop.writes.length;
  Object.defineProperty(resource, 'attributes', {get() {throw new Error('Unchanged resource inspected');}});
  Object.defineProperty(resource, 'children', {get() {throw new Error('Unchanged resource traversed');}});
  f.host.present(JSON.stringify(scene([{...rect, attrs: {...rect.attrs, width: '30'}},
    {tag: 'text', attrs: {}, text: 'Popup'}], {reset: false, definitions: [gradient('popup')]})));
  assert.equal(bank.children[0], resource); assert.equal(stop.writes.length, writes);
  assert.equal(f.surface.children.filter(node => node !== bank)[0], body);
  assert.equal(body.getAttribute('width'), '30'); assert.equal(bank.children.length, 2);
  f.host.present(JSON.stringify(scene([], {reset: false, length: 1, updates: []})));
  assert.deepEqual(f.surface.children.filter(node => node !== bank), [body]);
  assert.equal(bank.children.length, 2);
});

test('full snapshots reuse gradient identity and prune absent definitions', t => {
  const f = fixture(t), rect = {tag: 'rect', attrs: {fill: 'url(#face)'}};
  f.host.present(JSON.stringify(scene([rect], {definitions: [gradient('face'), gradient('gone')]})));
  const bank = f.surface.children[0], resource = bank.children[0], stop = resource.children[0];
  const body = f.surface.children[1], writes = stop.writes.length, gone = bank.children[1];
  f.host.present(JSON.stringify(scene([rect], {revision: 20, definitions: [gradient('face')]})));
  assert.equal(f.surface.children[0], bank); assert.equal(bank.children[0], resource);
  assert.equal(resource.children[0], stop); assert.equal(stop.writes.length, writes);
  assert.equal(gone.parentNode, null); assert.equal(bank.children.length, 1);
  assert.equal(f.surface.children[1], body);
  const changed = gradient('face', '#456'); changed.attrs.gradientTransform = 'rotate(45)';
  changed.children = [{tag: 'stop', attrs: {'stop-color': '#456'}}];
  f.host.present(JSON.stringify(scene([], {reset: false, length: 1, definitions: [changed]})));
  assert.equal(bank.children[0], resource); assert.equal(resource.children[0], stop);
  assert.equal(stop.getAttribute('offset'), null); assert.equal(stop.getAttribute('stop-color'), '#456');
  assert.equal(resource.children.length, 1); assert.equal(resource.getAttribute('gradientTransform'), 'rotate(45)');
  f.host.present(JSON.stringify(scene([rect], {revision: 30, definitions: [gradient('face')]})));
  assert.equal(resource.getAttribute('gradientTransform'), null); assert.equal(resource.children.length, 2);
});

test('gradient references are installed before body updates and retired afterwards', t => {
  const f = fixture(t), body = {tag: 'rect', attrs: {fill: 'url(#old)'}};
  f.host.present(JSON.stringify(scene([body], {definitions: [gradient('old')]})));
  const bank = f.surface.children[0], old = bank.children[0], rect = f.surface.children[1];
  const setAttribute = rect.setAttribute.bind(rect);
  rect.setAttribute = (name, value) => {
    if (name === 'fill') {
      assert.equal(old.parentNode, bank);
      assert.ok(bank.children.some(node => node.getAttribute('id') === 'new'));
    }
    setAttribute(name, value);
  };
  f.host.present(JSON.stringify(scene([{...body, attrs: {fill: 'url(#new)'}}], {
    reset: false, definitions: [gradient('new')], remove_definitions: ['old'],
  })));
  assert.equal(old.parentNode, null); assert.equal(bank.children.length, 1);
  assert.equal(bank.children[0].getAttribute('id'), 'new'); assert.equal(f.surface.children[1], rect);
});

test('definition banks can disappear and return without shifting bodies or crossing sessions', t => {
  const f = fixture(t), rect = {tag: 'rect', attrs: {fill: '#123'}};
  f.host.present(JSON.stringify(scene([rect])));
  const body = f.surface.children[0];
  f.host.present(JSON.stringify(scene([], {reset: false, length: 1, definitions: [gradient('first')]})));
  const firstBank = f.surface.children[1];
  assert.equal(firstBank.localName, 'defs'); assert.equal(f.surface.children[0], body);
  f.host.present(JSON.stringify(scene([rect], {revision: 20, definitions: []})));
  assert.deepEqual(f.surface.children, [body]); assert.equal(firstBank.parentNode, null);
  f.host.present(JSON.stringify(scene([], {reset: false, length: 1, definitions: [gradient('second')]})));
  const secondBank = f.surface.children[1];
  assert.notEqual(secondBank, firstBank); assert.equal(f.surface.children[0], body);
  f.host.present(JSON.stringify(scene([], {reset: false, length: 0, remove_definitions: ['second']})));
  assert.equal(f.surface.children.length, 0);
  f.host.present(JSON.stringify(scene([rect], {definitions: [gradient('last')]})));
  f.host.close(); f.host.open('Again', 640, 400, 1);
  assert.equal(f.surface.children.length, 0);
  f.host.present(JSON.stringify(scene([rect], {definitions: [gradient('last')]})));
  assert.equal(f.surface.children.length, 2);
  assert.equal(f.surface.children[0].children[0].getAttribute('id'), 'last');
});

test('scene deltas preserve unchanged DOM identity and replace only changed geometry', t => {
  const f = fixture(t), rect = {tag: 'rect', attrs: {x: '1', width: '20', fill: '#123456'}};
  f.host.present(JSON.stringify(scene([rect, {tag: 'text', attrs: {x: '8'}, text: 'Hello'}])));
  const first = f.surface.children[0], label = f.surface.children[1];
  f.host.present(JSON.stringify(scene([], {revision: 2, reset: false, length: 2,
    updates: [[1, {tag: 'text', attrs: {y: '10'}, text: 'Changed'}]]})));
  assert.equal(f.surface.children[0], first); assert.equal(f.surface.children[1], label);
  assert.equal(label.textContent, 'Changed'); assert.equal(label.getAttribute('x'), null);
  assert.equal(label.getAttribute('y'), '10'); assert.equal(f.host.frames, 2);
  assert.equal(f.surface.getAttribute('viewBox'), '0 0 640 400');
  assert.equal(f.doc.title, 'SVG fixture');
  f.host.present(JSON.stringify(scene([], {reset: false, length: 1,
    updates: [[0, {tag: 'circle', attrs: {r: '6'}}]]})));
  assert.equal(f.surface.children.length, 1); assert.notEqual(f.surface.children[0], first);
  assert.equal(f.surface.children[0].localName, 'circle');
});

test('SVG scene carries clips, gradients, transforms, text and image geometry without browser repaint rules', t => {
  const f = fixture(t);
  const defs = {tag: 'defs', attrs: {}, children: [
    {tag: 'clipPath', attrs: {id: 'clip'}, children: [{tag: 'rect', attrs: {width: '20', height: '30'}}]},
    {tag: 'linearGradient', attrs: {id: 'gradient', gradientUnits: 'userSpaceOnUse'}, children: [
      {tag: 'stop', attrs: {offset: '0', 'stop-color': '#fff', 'stop-opacity': '.5'}},
      {tag: 'stop', attrs: {offset: '1', 'stop-color': '#000'}},
    ]},
  ]};
  const group = {tag: 'g', attrs: {'clip-path': 'url(#clip)', transform: 'translate(12 14)'}, children: [
    {tag: 'rect', attrs: {width: '50', height: '60', fill: 'url(#gradient)'}},
    {tag: 'text', attrs: {'font-family': 'Pysual Mono', y: '12'}, text: '<plain text>'},
    {tag: 'image', attrs: {href: 'data:image/png;base64,AA==', width: '16', height: '8', 'data-pysual-image': '3'}},
  ]};
  f.host.present(JSON.stringify(scene([defs, group])));
  assert.equal(f.surface.children[0].children[1].children[0].getAttribute('stop-opacity'), '.5');
  const drawn = f.surface.children[1];
  assert.equal(drawn.getAttribute('transform'), 'translate(12 14)');
  assert.equal(drawn.children[1].textContent, '<plain text>');
  assert.equal(drawn.children[1].children.length, 0);
  assert.equal(drawn.children[2].getAttribute('href'), 'data:image/png;base64,AA==');
  assert.equal('rect' in f.host, false); assert.equal('surfaceCreate' in f.host, false);
});

test('scene reset and text/container transitions remove stale content', t => {
  const f = fixture(t);
  f.host.present(JSON.stringify(scene([{tag: 'g', attrs: {}, text: 'old'}])));
  const group = f.surface.children[0];
  f.host.present(JSON.stringify(scene([{tag: 'g', attrs: {}, children: [{tag: 'text', attrs: {}, text: 'new'}]}], {reset: false})));
  assert.equal(f.surface.children[0], group); assert.equal(group.textContent, 'new');
  f.host.present(JSON.stringify(scene([{tag: 'g', attrs: {}, text: 'only text'}], {reset: false})));
  assert.equal(group.children.length, 0); assert.equal(group.textContent, 'only text');
  f.host.present(JSON.stringify(scene([{tag: 'g', attrs: {}, text: 'reset'}])));
  assert.equal(f.surface.children[0], group); assert.equal(group.textContent, 'reset');
  f.host.close(); f.host.open('Again', 640, 400, 1);
  assert.equal(f.surface.children.length, 0); assert.equal(f.host.frames, 0);
});

test('full snapshots after missed revisions reuse nodes and replace obsolete scene state', t => {
  const f = fixture(t);
  const retained = {tag: 'g', attrs: {'clip-path': 'url(#clip)'}, children: [
    {tag: 'text', attrs: {x: '10', y: '20'}, text: 'Retained'},
    {tag: 'image', attrs: {href: 'data:image/png;base64,AA==', width: '20'}},
  ]};
  f.host.present(JSON.stringify(scene([retained, {tag: 'rect', attrs: {width: '40', fill: '#123'}}])));
  const group = f.surface.children[0], label = group.children[0], image = group.children[1];
  const writes = image.writes.length;
  for (let revision = 20; revision < 26; revision++) {
    f.host.present(JSON.stringify(scene([retained, {tag: 'rect', attrs: {width: '40', fill: '#456'}}], {revision})));
    assert.equal(f.surface.children[0], group); assert.equal(group.children[0], label);
    assert.equal(group.children[1], image); assert.equal(image.writes.length, writes);
  }
  f.host.present(JSON.stringify(scene([{tag: 'g', attrs: {}, children: [
    {tag: 'text', attrs: {y: '30'}, text: 'Updated'},
    {tag: 'circle', attrs: {r: '8'}},
  ]}], {revision: 40})));
  assert.equal(f.surface.children.length, 1); assert.equal(f.surface.children[0], group);
  assert.equal(group.getAttribute('clip-path'), null); assert.equal(group.children[0], label);
  assert.equal(label.getAttribute('x'), null); assert.equal(label.getAttribute('y'), '30');
  assert.equal(label.textContent, 'Updated'); assert.equal(group.children[1].localName, 'circle');
  assert.notEqual(group.children[1], image);
  f.host.present(JSON.stringify(scene([], {revision: 80})));
  assert.equal(f.surface.children.length, 0);
});

test('repeated scene updates reuse owned attribute and text state without DOM reads', t => {
  const f = fixture(t);
  const node = {tag: 'text', attrs: {x: '12', y: '20', fill: '#123'}, text: 'First'};
  f.host.present(JSON.stringify(scene([node])));
  const label = f.surface.children[0], initialWrites = label.writes.length;
  const surfaceWrites = f.surface.writes.length;
  Object.defineProperty(label, 'attributes', {get() {throw new Error('Unnecessary DOM attribute enumeration');}});
  label.getAttribute = () => {throw new Error('Unnecessary DOM attribute read');};
  f.host.present(JSON.stringify(scene([node], {reset: false})));
  assert.equal(label.writes.length, initialWrites);
  assert.equal(f.surface.writes.length, surfaceWrites);
  node.attrs.x = '30'; delete node.attrs.y; node.text = 'Second';
  f.host.present(JSON.stringify(scene([node], {reset: false})));
  assert.equal(label.attrs.get('x'), '30'); assert.equal(label.attrs.has('y'), false);
  assert.equal(label.textContent, 'Second'); assert.equal(label.writes.length, initialWrites + 1);
  f.host.present(JSON.stringify(scene([node], {reset: false, title: 'New title', width: 700})));
  assert.equal(f.doc.title, 'New title'); assert.equal(f.surface.getAttribute('viewBox'), '0 0 700 400');
});

test('editor frames avoid layout reads while resize and scrolling refresh its position', t => {
  const f = fixture(t), editor = f.doc.body.children.find(node => node.tag === 'textarea');
  const packet = scene([{tag: 'text', attrs: {}, text: 'Typing'}], {text_input: [10, 15, 100, 30]});
  f.host.present(JSON.stringify(packet));
  assert.equal(editor.style.left, '30px'); assert.equal(editor.style.top, '45px');
  let reads = 0, bounds = {left: 60, top: 70, width: 640, height: 400};
  f.surface.getBoundingClientRect = () => {reads++; return bounds;};
  f.surface.focus();
  f.host.present(JSON.stringify({...packet, reset: false}));
  assert.equal(reads, 0); assert.equal(f.doc.activeElement, editor);
  f.win.dispatchEvent(new Event('resize'));
  assert.equal(reads, 1); assert.equal(editor.style.left, '70px'); assert.equal(editor.style.top, '85px');
  bounds = {...bounds, left: 45, top: 50};
  f.win.dispatchEvent(new Event('scroll'));
  assert.equal(reads, 2); assert.equal(editor.style.left, '55px'); assert.equal(editor.style.top, '65px');
  f.host.textInput(20, 25, 80, 40);
  assert.equal(reads, 2); assert.equal(editor.style.left, '65px'); assert.equal(editor.style.top, '75px');
  assert.equal(editor.style.width, '80px'); assert.equal(editor.style.height, '40px');
  f.host.close(); f.win.dispatchEvent(new Event('scroll'));
  assert.equal(reads, 2);
});

test('SVG image errors report identifiers and closed clients detach their listener', t => {
  const f = fixture(t);
  const image = f.doc.createElementNS('http://www.w3.org/2000/svg', 'image');
  image.setAttribute('data-pysual-image', '7');
  const failure = () => { const event = new Event('error'); Object.defineProperty(event, 'target', {value: image}); f.surface.dispatchEvent(event); };
  failure(); assert.deepEqual(JSON.parse(f.host.poll()), [{kind: 'image_error', text: '7'}]);
  f.host.close(); failure(); assert.deepEqual(JSON.parse(f.host.poll()), []);
  f.host.open('Again', 640, 400, 1); f.host.poll(); failure();
  assert.deepEqual(JSON.parse(f.host.poll()), [{kind: 'image_error', text: '7'}]);
});

test('unchanged viewport preserves the SVG scene and DPR changes invalidate resources once', t => {
  const f = fixture(t);
  f.host.present(JSON.stringify(scene([{tag: 'text', attrs: {}, text: 'retained'}])));
  const child = f.surface.children[0], revision = f.host.resourceRevision;
  f.observers[0](); f.win.dispatchEvent(new Event('resize'));
  assert.equal(f.surface.children[0], child); assert.equal(f.host.resourceRevision, revision);
  assert.deepEqual(JSON.parse(f.host.poll()), []);
  f.win.getComputedStyle = () => ({paddingTop: '8px'}); f.observers[0]();
  assert.equal(f.host.resourceRevision, revision); assert.equal(JSON.parse(f.host.poll())[0].viewport.safe_area[0], 8);
  f.win.devicePixelRatio = 2; f.observers[0]();
  assert.equal(f.host.resourceRevision, revision + 1); assert.equal(f.host.renderScale, 2);
  assert.equal(f.surface.children[0], child); assert.equal(JSON.parse(f.host.poll())[0].viewport.scale, 2);
});

test('standalone decoder preserves bytes with native and bounded fallback paths', () => {
  const standalone = readFileSync(join(dirname(process.env.PYSUAL_TEST_HOST), 'standalone.js'), 'utf8');
  const decoder = standalone.match(/const bytes = (name => \{[\s\S]*?\n\});/)[1];
  class FallbackBytes extends Uint8Array { static fromBase64 = undefined; }
  let nativeCalls = 0;
  class NativeBytes extends Uint8Array {
    static fromBase64(encoded) {nativeCalls++; return new Uint8Array(Buffer.from(encoded, 'base64'));}
  }
  for (const length of [0, 1, 2, 3, 49151, 49152, 49153, 150000]) {
    const expected = Buffer.from(Array.from({length}, (_, i) => i % 256));
    const bundle = {files: {fixture: expected.toString('base64')}};
    for (const type of [FallbackBytes, NativeBytes]) {
      const chunks = [];
      const decode = new Function('bundle', 'Uint8Array', 'atob', `return ${decoder}`)(bundle, type,
        encoded => {chunks.push(encoded.length); return atob(encoded);});
      assert.deepEqual(Buffer.from(decode('fixture')), expected);
      assert.ok(chunks.every(size => size <= 65536));
      assert.throws(() => decode('absent'), /Missing embedded file/);
    }
  }
  assert.equal(nativeCalls, 8);
});

test('cancel and leave preserve the browser pointer identity', t => {
  const f = fixture(t);
  for (const [name, kind] of [['pointercancel', 'pointer_cancel'], ['pointerleave', 'pointer_leave']]) {
    f.surface.dispatchEvent(Object.assign(new Event(name), {
      pointerId: 7, pointerType: 'pen', clientX: 50, clientY: 70, button: 0,
    }));
    const [event] = JSON.parse(f.host.poll());
    assert.equal(event.kind, kind);
    assert.equal(event.pointer_id, 7);
    assert.equal(event.pointer_kind, 'pen');
    assert.equal(event.x, 30);
    assert.equal(event.y, 40);
  }
});

test('SVG context menu is owned only by the active host session', t => {
  const f = fixture(t);
  const active = new Event('contextmenu', {cancelable: true});
  f.surface.dispatchEvent(new Event('pointerleave'));
  assert.equal(JSON.parse(f.host.poll())[0].kind, 'pointer_leave');
  f.surface.dispatchEvent(active);
  assert.equal(active.defaultPrevented, true);
  f.host.close();
  const closed = new Event('contextmenu', {cancelable: true});
  f.surface.dispatchEvent(closed);
  assert.equal(closed.defaultPrevented, false);
});
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

function fixture(t) {
  const observers = [];
  let failDialog = false;
  class Element extends EventTarget {
    constructor(tag) {
      super(); this.tag = this.localName = tag; this.children = []; this.style = {};
      this.value = ''; this._text = ''; this.disabled = false; this.attrs = new Map(); this.writes = [];
    }
    get attributes() { return [...this.attrs].map(([name, value]) => ({name, value})); }
    setAttribute(name, value) { this.attrs.set(name, String(value)); this.writes.push([name, String(value)]); }
    getAttribute(name) { return this.attrs.get(name) ?? null; }
    removeAttribute(name) { this.attrs.delete(name); }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this.replaceChildren(); this._text = String(value); }
    get childNodes() { return [...this.children, ...(this._text ? [{nodeType: 3, remove: () => {this._text = '';}}] : [])]; }
    get parentNode() { return this.parent; }
    get lastElementChild() { return this.children.at(-1); }
    dispatchEvent(event) { const result = super.dispatchEvent(event); this[`on${event.type}`]?.(event); return result; }
    close() {}
    append(...children) { for (const child of children) { child.remove(); child.parent = this; this.children.push(child); } }
    appendChild(child) { this.append(child); return child; }
    replaceChildren(...children) { for (const child of this.children) child.parent = null; this.children = []; this._text = ''; this.append(...children); }
    replaceWith(child) { const parent = this.parent; const index = parent.children.indexOf(this); this.parent = null; child.parent = parent; parent.children[index] = child; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); this.parent = null; }
    focus() { doc.activeElement = this; }
    blur() { if (doc.activeElement === this) doc.activeElement = null; this.dispatchEvent(new Event('blur')); }
    select() { this.selectionStart = 0; this.selectionEnd = this.value.length; }
    getBoundingClientRect() { return {left: 20, top: 30, width: 640, height: 400}; }
    showModal() { if (failDialog) { failDialog = false; throw new Error('Cannot open dialog'); } }
    click() { if (!this.disabled) this.dispatchEvent(new Event('click')); }
    setPointerCapture() {}
    hasPointerCapture() { return false; }
  }
  const doc = new EventTarget();
  doc.body = new Element('body'); doc.fonts = new Set(); doc.createElement = tag => new Element(tag);
  doc.createElementNS = (namespace, tag) => { assert.equal(namespace, 'http://www.w3.org/2000/svg'); return new Element(tag); };
  const win = new EventTarget(); win.devicePixelRatio = 1;
  const globals = {document: doc, window: win,
    ResizeObserver: class { constructor(callback) { observers.push(callback); } observe() {} disconnect() {} }};
  for (const [name, value] of Object.entries(globals)) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, {configurable: true, writable: true, value});
    t.after(() => previous ? Object.defineProperty(globalThis, name, previous) : delete globalThis[name]);
  }
  const surface = new Element('svg');
  const host = createHost(surface);
  host.open('Fixture', 640, 400, 1); host.poll();
  t.after(() => host.close());
  return {host, surface, observers, win, doc,
    dialog: () => doc.body.children.find(node => node.tag === 'dialog'),
    button(label) { return this.dialog().children.find(node => node.className === 'actions').children.find(node => node.tag === 'button' && node.textContent === label); },
    failNextDialog() { failDialog = true; },
  };
}

test('rejected browser opening retains the active surface and input', t => {
  const f = fixture(t), editor = f.doc.body.children[0];
  f.host.present(JSON.stringify(scene([{tag: 'text', attrs: {}, text: 'First'}])));
  assert.throws(() => f.host.open('Second', 640, 400, 1), /already active/);
  assert.equal(f.doc.body.children[0], editor);
  f.host.present(JSON.stringify(scene([{tag: 'text', attrs: {}, text: 'Still active'}])));
  assert.equal(f.surface.textContent, 'Still active');
  f.surface.dispatchEvent(Object.assign(new Event('pointerdown'), {clientX: 21, clientY: 31, button: 0}));
  assert.equal(JSON.parse(f.host.poll())[0].kind, 'pointer_down');
});

test('partial client construction removes its DOM and listeners before reopening', t => {
  const f = fixture(t), Observer = globalThis.ResizeObserver;
  f.host.close();
  let disconnected = 0;
  globalThis.ResizeObserver = class extends Observer {
    observe() { throw new Error('Cannot observe surface'); }
    disconnect() { disconnected++; }
  };
  assert.throws(() => f.host.open('Failed', 640, 400, 1), /Cannot observe surface/);
  assert.equal(disconnected, 1);
  assert.equal(f.doc.body.children.length, 0);
  const event = new Event('contextmenu', {cancelable: true});
  f.surface.dispatchEvent(event);
  assert.equal(event.defaultPrevented, false);
  globalThis.ResizeObserver = Observer;
  f.host.open('Reopened', 640, 400, 1); f.host.poll();
  assert.equal(f.doc.body.children.filter(node => node.tag === 'textarea').length, 1);
  f.surface.dispatchEvent(Object.assign(new Event('pointerdown'), {clientX: 21, clientY: 31, button: 0}));
  assert.equal(JSON.parse(f.host.poll()).length, 1);
});

test('new image resource identities retry the same URL without changing other nodes', t => {
  const f = fixture(t), url = 'preview.png';
  const picture = id => ({tag: 'image', image: id, attrs: {'data-pysual-image': id}});
  const label = {tag: 'text', attrs: {}, text: 'Unchanged'};
  f.host.present(JSON.stringify(scene([picture('1'), label], {image_sources: {'1': url}})));
  const oldImage = f.surface.children[0], oldLabel = f.surface.children[1];
  f.host.present(JSON.stringify(scene([picture('2'), label], {
    reset: false, image_sources: {'2': url}, remove_images: ['1'],
  })));
  const freshImage = f.surface.children[0];
  assert.notEqual(freshImage, oldImage); assert.equal(oldImage.parentNode, null);
  assert.equal(freshImage.getAttribute('href'), url);
  assert.equal(f.surface.children[1], oldLabel);
  f.host.present(JSON.stringify(scene([picture('2'), label], {reset: false})));
  assert.equal(f.surface.children[0], freshImage);
});

test('image source references preserve DOM images across geometry deltas and resets', t => {
  const f = fixture(t), source = 'data:image/png;base64,AA==';
  const picture = x => ({tag: 'g', attrs: {}, children: [{tag: 'image', image: '7', attrs: {
    x: String(x), y: '0', width: '30', height: '20', preserveAspectRatio: 'xMidYMid slice',
    'data-pysual-image': '7',
  }}]});
  f.host.present(JSON.stringify(scene([picture(0), picture(40)], {image_sources: {'7': source}})));
  const first = f.surface.children[0].children[0], second = f.surface.children[1].children[0];
  assert.equal(first.getAttribute('href'), source);
  assert.equal(second.getAttribute('href'), source);
  assert.equal(first.getAttribute('preserveAspectRatio'), 'xMidYMid slice');
  const writes = first.writes.filter(([name]) => name === 'href').length;
  f.host.present(JSON.stringify(scene([], {reset: false, length: 2, updates: [[0, picture(1)]]})));
  assert.equal(f.surface.children[0].children[0], first);
  assert.equal(first.getAttribute('x'), '1');
  assert.equal(first.writes.filter(([name]) => name === 'href').length, writes);
  f.host.present(JSON.stringify(scene([picture(1), picture(40)], {image_sources: {'7': source}})));
  assert.equal(f.surface.children[0].children[0], first);
  assert.equal(first.writes.filter(([name]) => name === 'href').length, writes);
  const error = new Event('error'); Object.defineProperty(error, 'target', {value: first});
  f.surface.dispatchEvent(error);
  assert.deepEqual(JSON.parse(f.host.poll()), [{kind: 'image_error', text: '7'}]);
  f.host.present(JSON.stringify(scene([], {reset: false, remove_images: ['7']})));
  assert.equal(f.surface.children.length, 0);
  f.host.close(); f.host.open('Again', 640, 400, 1); f.host.poll();
  const next = 'data:image/png;base64,AQ==';
  f.host.present(JSON.stringify(scene([picture(0)], {image_sources: {'7': next}})));
  assert.equal(f.surface.children[0].children[0].getAttribute('href'), next);
});

test('navigation keys prevent page scrolling while text input keeps ordinary spaces', t => {
  const f = fixture(t);
  for (const key of ['PageUp', 'PageDown', 'F2', 'F10', ' ']) {
    const event = Object.assign(new Event('keydown', {cancelable: true}), {key});
    f.surface.dispatchEvent(event);
    assert.equal(event.defaultPrevented, true);
    assert.equal(JSON.parse(f.host.poll())[0].key, key === ' ' ? 'Space' : key);
  }
  const input = f.doc.body.children.find(node => node.tag === 'textarea');
  f.host.textInput(0, 0, 100, 30);
  const space = Object.assign(new Event('keydown', {cancelable: true}), {key: ' '});
  input.dispatchEvent(space);
  assert.equal(space.defaultPrevented, false);
});

test('input state is cleared on host close/reopen and on leaving text input', t => {
  const f = fixture(t);
  const input = f.doc.body.children.find(node => node.tag === 'textarea');
  f.host.textInput(0, 0, 100, 30);
  input.value = 'unfinished composition';
  f.host.textInput(0, 0, -1, 0);
  assert.equal(input.value, '');
  input.value = 'old session';
  f.host.close(); f.host.open('Next', 640, 400, 1); f.host.poll();
  assert.equal(input.value, '');
});

test('text viewport updates leave focus in the shared dialog until it closes', async t => {
  const f = fixture(t);
  const editor = f.doc.body.children.find(node => node.tag === 'textarea');
  const pending = f.host.openTextFile(1024);
  const picker = f.dialog().children.find(node => node.tag === 'input');
  picker.focus();
  f.host.textInput(10, 20, 100, 30);
  assert.equal(f.doc.activeElement, picker);
  f.button('Cancel').click();
  assert.equal(await pending, 'null');
  assert.equal(f.doc.activeElement, editor);
  f.host.textInput(10, 20, 100, 30);
  assert.equal(f.doc.activeElement, editor);
});

test('ending text input during a dialog restores surface focus and clears keyboard coverage', async t => {
  const f = fixture(t);
  f.host.textInput(10, 20, 100, 30);
  const pending = f.host.openTextFile(1024);
  const picker = f.dialog().children.find(node => node.tag === 'input'); picker.focus();
  f.host.textInput(0, 0, -1, 0);
  assert.equal(f.doc.activeElement, picker);
  f.button('Cancel').click(); await pending;
  assert.equal(f.doc.activeElement, f.surface);
  assert.equal(JSON.parse(f.host.viewport).keyboard_occlusion, 0);
});

test('cancelled save does not open or write a late destination', async t => {
  const f = fixture(t), picker = deferred(); let opened = 0;
  f.win.showSaveFilePicker = () => picker.promise;
  const operation = f.host.saveTextFile('snapshot', 'sample.txt');
  f.button('Choose destination').click();
  f.button('Cancel').click();
  assert.equal(await operation, null);
  picker.resolve({name: 'sample.txt', async createWritable() { opened++; return {async write() {}, async close() {}}; }});
  await tick();
  assert.equal(opened, 0);
});

test('shutdown during stream creation aborts without writing', async t => {
  const f = fixture(t), writable = deferred(); let writes = 0, commits = 0, aborts = 0;
  f.win.showSaveFilePicker = async () => ({name: 'sample.txt', createWritable: () => writable.promise});
  const operation = f.host.saveTextFile('snapshot', 'sample.txt');
  f.button('Choose destination').click();
  await tick(); f.host.close();
  assert.equal(await operation, null);
  writable.resolve({async write() { writes++; }, async close() { commits++; }, async abort() { aborts++; }});
  await tick();
  assert.equal(writes, 0); assert.equal(commits, 0); assert.equal(aborts, 1);
});

test('cancel during write aborts the stream before commit', async t => {
  const f = fixture(t), writing = deferred(); let commits = 0, aborts = 0;
  f.win.showSaveFilePicker = async () => ({name: 'sample.txt', async createWritable() {
    return {write: () => writing.promise, async close() { commits++; }, async abort() { aborts++; }};
  }});
  const operation = f.host.saveTextFile('snapshot', 'sample.txt');
  f.button('Choose destination').click(); await tick();
  f.button('Cancel').click(); assert.equal(await operation, null);
  writing.resolve(); await tick();
  assert.equal(commits, 0); assert.equal(aborts, 1);
});

test('repeated destination actions create one save and preserve successful output', async t => {
  const f = fixture(t), picker = deferred(); let pickers = 0; const written = [];
  f.win.showSaveFilePicker = () => { pickers++; return picker.promise; };
  const operation = f.host.saveTextFile('snapshot', 'sample.txt');
  const button = f.button('Choose destination');
  button.dispatchEvent(new Event('click')); button.dispatchEvent(new Event('click'));
  assert.equal(pickers, 1);
  picker.resolve({name: 'sample.txt', async createWritable() { return {
    async write(text) { written.push(text); }, async close() { written.push('committed'); },
  }; }});
  assert.equal(await operation, 'sample.txt');
  assert.deepEqual(written, ['snapshot', 'committed']);
});

test('failed dialog setup releases its session reservation', async t => {
  const f = fixture(t);
  f.win.showSaveFilePicker = async () => {};
  f.failNextDialog();
  await assert.rejects(f.host.saveTextFile('snapshot', 'sample.txt'), /Cannot open dialog/);
  assert.equal(f.dialog(), undefined);
  const next = f.host.saveTextFile('next', 'next.txt');
  f.button('Cancel').click(); assert.equal(await next, null);
});

test('late native text/composition is ignored after text focus is cleared', t => {
  const f = fixture(t), input = f.doc.body.children.find(node => node.tag === 'textarea');
  f.host.textInput(0, 0, 100, 30);f.host.poll();
  input.value = 'typed'; input.dispatchEvent(new Event('input'));
  assert.deepEqual(JSON.parse(f.host.poll()), [{kind: 'text', text: 'typed'}]);
  f.host.textInput(0, 0, -1, 0);f.host.poll();
  input.value = 'late'; input.dispatchEvent(new Event('input'));
  for (const kind of ['compositionupdate', 'compositionend']) {
    input.dispatchEvent(Object.assign(new Event(kind), {data: 'late'}));
  }
  assert.deepEqual(JSON.parse(f.host.poll()), []);
  assert.equal(input.value, '');
});

test('failed writes preserve the original error and allow a successful retry', async t => {
  const f = fixture(t); let attempt = 0;
  f.win.showSaveFilePicker = async () => ({name: 'sample.txt', async createWritable() {
    const fail = attempt++ === 0;
    return {
      async write() { if (fail) throw new Error('disk denied'); },
      async abort() { throw new Error('already closed'); },
      async close() {},
    };
  }});
  const operation = f.host.saveTextFile('snapshot', 'sample.txt');
  f.button('Choose destination').click(); await tick();
  assert.equal(f.dialog().children.find(node => node.tag === 'p').textContent, 'Save failed: disk denied');
  assert.equal(f.button('Choose destination').disabled, false);
  f.button('Choose destination').click();
  assert.equal(await operation, 'sample.txt');
});

test('wheel pixel, line and page modes normalize to consistent scroll units', t => {
  const f=fixture(t);
  for(const [deltaMode,deltaY,expected] of [[0,100,1],[1,6.25,1],[2,0.25,1],[0,12.5,0.125]]){
    f.surface.dispatchEvent(Object.assign(new Event('wheel',{cancelable:true}),{deltaMode,deltaY,clientX:50,clientY:70}));
    const [event]=JSON.parse(f.host.poll());assert.equal(event.delta,expected);assert.equal(event.x,30);assert.equal(event.y,40);
  }
});

test('wheel input preserves the dominant axis, direction and modifiers', t => {
  const f = fixture(t);
  const cases = [
    [{deltaX: 100}, 1, true], [{deltaX: -100}, -1, true],
    [{deltaX: 100, deltaY: 25}, 1, true],
    [{deltaX: 25, deltaY: 100}, 1, false],
    [{deltaX: 100, deltaY: 100}, 1, false],
    [{deltaY: -100, shiftKey: true, ctrlKey: true}, -1, true],
    [{deltaX: -2, deltaMode: 1}, -0.32, true],
    [{deltaX: 0.25, deltaMode: 2, metaKey: true}, 1.6, true],
  ];
  for (const [input, delta, shift] of cases) {
    const wheel = Object.assign(new Event('wheel', {cancelable: true}), {
      clientX: 50, clientY: 70, deltaX: 0, deltaY: 0, deltaMode: 0,
      shiftKey: false, ctrlKey: false, metaKey: false, ...input,
    });
    f.surface.dispatchEvent(wheel);
    const events = JSON.parse(f.host.poll());
    assert.equal(events.length, 1);
    assert.deepEqual(events[0], {kind: 'wheel', x: 30, y: 40, delta, shift,
      ctrl: Boolean(input.ctrlKey || input.metaKey)});
    assert.equal(wheel.defaultPrevented, true);
  }
  f.surface.dispatchEvent(Object.assign(new Event('wheel', {cancelable: true}), {deltaX: 0, deltaY: 0}));
  assert.deepEqual(JSON.parse(f.host.poll()), []);
});

test('viewport records safe areas, focused keyboard occlusion and suspension', t => {
  const f=fixture(t),visual=new EventTarget();
  Object.assign(visual,{height:230,offsetTop:0,scale:1});
  f.win.visualViewport=visual;f.win.getComputedStyle=()=>({paddingTop:'8px',paddingRight:'4px',paddingBottom:'6px',paddingLeft:'2px'});
  f.host.close();f.host.open('Viewport',640,400,1);f.host.poll();
  f.host.textInput(1,2,100,30);
  const [event]=JSON.parse(f.host.poll());assert.equal(event.kind,'viewport');
  assert.deepEqual(event.viewport.safe_area,[8,4,6,2]);assert.equal(event.viewport.keyboard_occlusion,200);
  visual.dispatchEvent(new Event('resize'));assert.deepEqual(JSON.parse(f.host.poll()),[]);
  visual.scale=2;visual.dispatchEvent(new Event('resize'));assert.equal(JSON.parse(f.host.poll())[0].viewport.keyboard_occlusion,0);
  f.doc.hidden=true;f.doc.dispatchEvent(new Event('visibilitychange'));
  assert.deepEqual(JSON.parse(f.host.poll()).map(e=>e.kind),['blur','suspend']);
  f.doc.hidden=false;f.doc.dispatchEvent(new Event('visibilitychange'));
  assert.deepEqual(JSON.parse(f.host.poll()).map(e=>e.kind),['resume']);
  f.host.close();visual.dispatchEvent(new Event('resize'));assert.deepEqual(JSON.parse(f.host.poll()),[]);
});

function memoryStorage() {
  const databases=new Map(),transactions=[];let connections=0;
  const indexedDB={open(name){const records=databases.get(name)||new Map();databases.set(name,records);const request={};setImmediate(()=>{
    connections++;
    request.result={objectStoreNames:{contains:()=>true},close(){connections--},transaction(){
      const staged=new Map(records);let pending=0,finished=false;
      const transaction={abort(){if(finished)return;finished=true;transaction.onabort?.()},objectStore:()=>store};
      transactions.push(transaction);
      const schedule=action=>{const request={};pending++;setImmediate(()=>{if(finished)return;action(request);pending--;if(!pending&&!finished){finished=true;records.clear();for(const [k,v] of staged)records.set(k,v);transaction.oncomplete?.()}});return request};
      const store={get(key){return schedule(request=>{request.result=staged.get(key);request.onsuccess?.()})},
        put(value,key){return schedule(()=>staged.set(key,value))},delete(key){return schedule(()=>staged.delete(key))},
        openCursor(){const entries=[...staged],request={};let index=0;
          const advance=()=>schedule(()=>{const entry=entries[index++];request.result=entry?{key:entry[0],value:entry[1],continue:advance}:null;request.onsuccess?.()});advance();return request}};
      return transaction;
    }};request.onsuccess?.();});return request}};
  return {indexedDB,databases,get records(){return databases.get('pysual-sessions-v2:default')},transactions,get connections(){return connections}};
}

test('JSON sessions preserve null and literal jsnull and isolate application quotas', async t => {
  const f=fixture(t),storage=memoryStorage();f.win.indexedDB=storage.indexedDB;
  f.host.setSessionNamespace('apps.First');
  assert.equal(await f.host.readSessionJSON('code'), 'null');
  await f.host.writeSessionJSON('code', JSON.stringify('jsnull'));
  assert.equal(await f.host.readSessionJSON('code'), '"jsnull"');
  for(let i=0;i<31;i++)await f.host.writeSession(`entry${i}`, 'first');
  await assert.rejects(f.host.writeSession('overflow','first'),/32 entries/);
  f.host.setSessionNamespace('apps.Second');
  assert.equal(await f.host.readSessionJSON('code'), 'null');
  await f.host.writeSessionJSON('code', JSON.stringify('second'));
  await f.host.writeSessionJSON('code','null');
  assert.equal(await f.host.readSessionJSON('code'), 'null');
  f.host.close();f.host.open('Again',640,400,1);f.host.setSessionNamespace('apps.First');
  assert.equal(await f.host.readSessionJSON('code'), '"jsnull"');
  assert.equal(storage.databases.size,2);
});

test('Python cancellation closes dialogs, revokes downloads and permits another operation', async t => {
  const f=fixture(t),revoked=[];
  const create=URL.createObjectURL,revoke=URL.revokeObjectURL;
  URL.createObjectURL=()=> 'blob:cancelled';URL.revokeObjectURL=url=>revoked.push(url);
  t.after(()=>{URL.createObjectURL=create;URL.revokeObjectURL=revoke});
  const pending=f.host.saveTextFile('draft','draft.txt',81);
  const rejected=assert.rejects(pending,/cancelled/);
  f.host.cancelOperation(999);assert.ok(f.dialog());
  f.host.cancelOperation(81);await rejected;
  assert.equal(f.dialog(),undefined);assert.deepEqual(revoked,['blob:cancelled']);
  const next=f.host.openTextFile(1024,82);
  f.dialog().children.find(node=>node.tag==='input').files=[{name:'next.txt',size:4,arrayBuffer:async()=>new TextEncoder().encode('next').buffer}];
  f.dialog().children.find(node=>node.tag==='input').dispatchEvent(new Event('change'));
  assert.deepEqual(JSON.parse(await next),{name:'next.txt',text:'next'});
  const url=f.host.openUrl('https://example.org',83), urlRejected=assert.rejects(url,/cancelled/);
  f.host.cancelOperation(83);await urlRejected;assert.equal(f.dialog(),undefined);
});

test('cancelling a pending clipboard permission prevents a late fallback dialog', async t => {
  const f=fixture(t),permission=deferred();
  const previous=Object.getOwnPropertyDescriptor(globalThis,'navigator');
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{clipboard:{readText:()=>permission.promise}}});
  t.after(()=>previous?Object.defineProperty(globalThis,'navigator',previous):delete globalThis.navigator);
  const pending=f.host.readClipboard(84), rejected=assert.rejects(pending,/cancelled/);
  f.host.cancelOperation(84);await rejected;permission.resolve(null);await tick();
  assert.equal(f.dialog(),undefined);
});

test('persistent sessions survive host reload, bound storage and support deletion', async t => {
  const f=fixture(t),storage=memoryStorage();f.win.indexedDB=storage.indexedDB;
  assert.equal(await f.host.readSession('code'),null);await f.host.writeSession('code','draft café');
  f.host.close();f.host.open('Reload',640,400,1);f.host.poll();assert.equal(await f.host.readSession('code'),'draft café');
  await f.host.writeSession('code',null);assert.equal(await f.host.readSession('code'),null);
  storage.records.set('existing',{text:'old',bytes:32*1024*1024});
  await assert.rejects(f.host.writeSession('code','next'),/32 MiB/);assert.equal(storage.records.has('code'),false);
  storage.records.clear();for(let i=0;i<32;i++)storage.records.set(`key${i}`,{text:'x',bytes:1});
  await assert.rejects(f.host.writeSession('code','next'),/32 entries/);
  await assert.rejects(f.host.writeSession('../escape','x'),/key/);
  await assert.rejects(f.host.writeSession('code','é'.repeat(4*1024*1024+1)),/8 MiB/);
  assert.equal(storage.connections,0);
});

test('session storage denial and closed sessions reject explicitly', async t => {
  const f=fixture(t);
  await assert.rejects(f.host.readSession('code'),/unsupported/);
  f.win.indexedDB={open(){throw new Error('storage denied')}};
  await assert.rejects(f.host.writeSession('code','draft'),/denied/);
  f.host.close();await assert.rejects(f.host.readSession('code'),/closed/);
});

test('URL opening uses a user gesture and preserves blocked outcome for retry', async t => {
  const f=fixture(t);let opened=0;f.win.open=()=>{opened++;return null};
  const operation=f.host.openUrl('https://example.org/path');assert.equal(opened,0);
  f.button('Open in browser').click();assert.match(f.dialog().children.find(n=>n.tag==='p').textContent,/blocked/);
  const destination={opener:'source'};f.win.open=()=>destination;f.button('Open in browser').click();
  assert.equal(await operation,true);assert.equal(destination.opener,null);
  await assert.rejects(f.host.openUrl('javascript:alert(1)'),/scheme/);
});

test('closing a pending storage transaction aborts without a late draft commit', async t => {
  const f=fixture(t),storage=memoryStorage();f.win.indexedDB=storage.indexedDB;
  const operation=f.host.writeSession('code','late draft');await tick();
  f.host.close();await assert.rejects(operation,/storage failed/);await tick();
  assert.equal(storage.records.has('code'),false);assert.equal(storage.connections,0);
});

test('a blocked database open cannot later execute an already rejected write', async t => {
  const f=fixture(t),request={};let transactions=0,closed=0;
  f.win.indexedDB={open:()=>request};const operation=f.host.writeSession('code','late draft');
  request.onblocked();await assert.rejects(operation,/blocked/);
  request.result={close(){closed++},transaction(){transactions++}};request.onsuccess();
  assert.equal(transactions,0);assert.equal(closed,1);
});

test('cancelling a session operation leaves the host and other operations usable', async t => {
  const f=fixture(t),storage=memoryStorage();f.win.indexedDB=storage.indexedDB;
  const operation=f.host.writeSession('code','late draft',41);await tick();
  f.host.cancelSession(41);await assert.rejects(operation,/storage failed/);await tick();
  assert.equal(storage.records.has('code'),false);assert.equal(storage.connections,0);
  await f.host.writeSession('code','current draft',42);
  assert.equal(await f.host.readSession('code',43),'current draft');
});

test('pending browser storage opens have a finite reservation budget', async t => {
  const f=fixture(t),requests=[];f.win.indexedDB={open(){const request={};requests.push(request);return request}};
  const pending=Array.from({length:32},(_,index)=>f.host.readSession('code',index));
  await assert.rejects(f.host.readSession('code',33),/pending/);
  f.host.close();for(const operation of pending)await assert.rejects(operation,/closed/);
  f.host.open('Restart',640,400,1);const again=f.host.readSession('code',34);
  f.host.cancelSession(34);await assert.rejects(again,/closed/);
});

test('stale resize observers cannot mutate a restarted host viewport', t => {
  const f=fixture(t),old=f.observers[0];f.host.close();f.host.open('Restart',640,400,1);f.host.poll();
  const revision=f.host.resourceRevision;old();
  assert.equal(f.host.resourceRevision,revision);assert.deepEqual(JSON.parse(f.host.poll()),[]);
});

test('unresponsive storage opens time out and release their reservation', async t => {
  const f=fixture(t),callbacks=[],requests=[];
  t.mock.method(globalThis,'setTimeout',callback=>{callbacks.push(callback);return callbacks.length});
  t.mock.method(globalThis,'clearTimeout',()=>{});
  f.win.indexedDB={open(){const request={};requests.push(request);return request}};
  const operation=f.host.readSession('code',1);callbacks[0]();
  await assert.rejects(operation,/timed out/);
  const retry=f.host.readSession('code',1);f.host.cancelSession(1);await assert.rejects(retry,/closed/);
});

test('ending text input immediately removes stale keyboard occlusion', t => {
  const f=fixture(t),visual=new EventTarget();Object.assign(visual,{height:230,offsetTop:0,scale:1});f.win.visualViewport=visual;
  f.host.close();f.host.open('Keyboard',640,400,1);f.host.poll();
  f.host.textInput(0,0,100,30);assert.equal(JSON.parse(f.host.poll())[0].viewport.keyboard_occlusion,200);
  f.host.textInput(0,0,-1,0);assert.equal(JSON.parse(f.host.poll())[0].viewport.keyboard_occlusion,0);
});

test('late callbacks from a cancelled session cannot release a new operation with its ID', async t => {
  const f=fixture(t),requests=[];f.win.indexedDB={open(){const request={};requests.push(request);return request}};
  const old=f.host.readSession('code',1);f.host.cancelSession(1);await assert.rejects(old,/closed/);
  const current=f.host.readSession('code',1);
  requests[0].result={close(){}};requests[0].onsuccess();
  f.host.cancelSession(1);await assert.rejects(current,/closed/);
});

test('native paste inserts once without forwarding Ctrl or Cmd V and listeners close cleanly', t => {
  const f = fixture(t);
  let input = f.doc.body.children.find(node => node.tag === 'textarea');
  for (const modifier of ['ctrlKey', 'metaKey']) {
    f.host.textInput(0, 0, 100, 30); f.host.poll();
    const key = Object.assign(new Event('keydown', {cancelable: true}), {key: 'v', [modifier]: true});
    input.dispatchEvent(key);
    assert.equal(key.defaultPrevented, false);
    assert.deepEqual(JSON.parse(f.host.poll()), []);
    const paste = Object.assign(new Event('paste', {cancelable: true}), {clipboardData: {getData: () => 'café 😀'}});
    input.dispatchEvent(paste);
    assert.equal(paste.defaultPrevented, true);
    assert.deepEqual(JSON.parse(f.host.poll()), [{kind: 'text', text: 'café 😀', paste: true}]);
    f.host.close();
    const closed = Object.assign(new Event('paste', {cancelable: true}), {clipboardData: {getData: () => 'late'}});
    input.dispatchEvent(closed);
    assert.equal(closed.defaultPrevented, false);
    f.host.open('Reopened', 640, 400, 1); f.host.poll();
    input = f.doc.body.children.find(node => node.tag === 'textarea');
  }
});

test('AltGraph characters stay text while Ctrl and Cmd retain shortcut behavior', t => {
  const f = fixture(t), input = f.doc.body.children.find(node => node.tag === 'textarea');
  f.host.textInput(0, 0, 100, 30); f.host.poll();
  for (const key of ['@', '€', 'v']) {
    for (const kind of ['keydown', 'keyup']) {
      const event = Object.assign(new Event(kind, {cancelable: true}), {
        key, ctrlKey: true, altKey: true, metaKey: false,
        getModifierState: modifier => modifier === 'AltGraph',
      });
      input.dispatchEvent(event);
      assert.equal(event.defaultPrevented, false);
      assert.deepEqual(JSON.parse(f.host.poll()), [{kind: kind === 'keydown' ? 'key_down' : 'key_up', key, ctrl: false}]);
    }
    input.value = key; input.dispatchEvent(new Event('input'));
    assert.deepEqual(JSON.parse(f.host.poll()), [{kind: 'text', text: key}]);
  }
  for (const modifier of ['ctrlKey', 'metaKey']) {
    const event = Object.assign(new Event('keydown', {cancelable: true}), {key: 'c', [modifier]: true});
    input.dispatchEvent(event);
    assert.equal(event.defaultPrevented, true);
    assert.equal(JSON.parse(f.host.poll())[0].ctrl, true);
  }
});

test('input-event paste keeps its undo boundary without marking batched typing', t => {
  const f = fixture(t), input = f.doc.body.children.find(node => node.tag === 'textarea');
  f.host.textInput(0, 0, 100, 30); f.host.poll();
  for (const [inputType, value, expected] of [
    ['insertText', 'ab', {kind: 'text', text: 'ab'}],
    ['insertFromPaste', 'P', {kind: 'text', text: 'P', paste: true}],
  ]) {
    input.value = value;
    input.dispatchEvent(Object.assign(new Event('input'), {inputType}));
    assert.deepEqual(JSON.parse(f.host.poll()), [expected]);
    assert.equal(input.value, '');
  }
});

test('paste validates decoded UTF-8 bytes and composition commits once', t => {
  const f = fixture(t), input = f.doc.body.children.find(node => node.tag === 'textarea');
  f.host.textInput(0, 0, 100, 30); f.host.poll();
  input.dispatchEvent(Object.assign(new Event('paste', {cancelable: true}), {clipboardData: {getData: () => 'é'.repeat(4 * 1024 * 1024 + 1)}}));
  assert.equal(JSON.parse(f.host.poll())[0].kind, 'resource_error');
  input.dispatchEvent(new Event('compositionstart'));
  input.value = '漢'; input.dispatchEvent(new Event('input'));
  assert.deepEqual(JSON.parse(f.host.poll()), []);
  input.dispatchEvent(Object.assign(new Event('compositionend'), {data: '漢'}));
  input.value = '漢'; input.dispatchEvent(new Event('input'));
  assert.deepEqual(JSON.parse(f.host.poll()), [{kind: 'composition', text: ''}, {kind: 'text', text: '漢'}]);
});

test('denied programmatic paste offers an explicit field and cancellation', async t => {
  const f = fixture(t);
  const original = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  Object.defineProperty(globalThis, 'navigator', {configurable: true, value: {clipboard: {readText: async () => {throw new Error('Denied');}}}});
  t.after(() => original ? Object.defineProperty(globalThis, 'navigator', original) : delete globalThis.navigator);
  const pasted = f.host.readClipboard(); await tick();
  f.dialog().children.find(node => node.tag === 'textarea').value = 'fallback';
  f.button('Use pasted text').click();
  assert.equal(await pasted, 'fallback');
  const cancelled = f.host.readClipboard(); await tick(); f.button('Cancel').click();
  await assert.rejects(cancelled, /cancelled/);
});

function clipboard(t, methods) {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  Object.defineProperty(globalThis, 'navigator', {configurable: true, value: {clipboard: methods}});
  t.after(() => original ? Object.defineProperty(globalThis, 'navigator', original) : delete globalThis.navigator);
}

test('denied copy supplies the original text and only succeeds after a copy action', async t => {
  const f = fixture(t);
  clipboard(t, {writeText: async () => {throw new Error('Denied');}});
  let settled = false;
  const pending = f.host.writeClipboard('café 😀', 91).then(value => {settled = true; return value;});
  await tick();
  const field = f.dialog().children.find(node => node.tag === 'textarea');
  assert.equal(field.value, 'café 😀'); assert.equal(field.readOnly, true);
  assert.equal(settled, false);
  f.button('Copy').click(); await tick();
  assert.equal(settled, false);
  assert.deepEqual([field.selectionStart, field.selectionEnd], [0, field.value.length]);
  const copied = [], event = Object.assign(new Event('copy', {cancelable: true}), {
    clipboardData: {setData: (...args) => copied.push(args)},
  });
  field.dispatchEvent(event);
  assert.equal(await pending, true);
  assert.deepEqual(copied, [['text/plain', 'café 😀']]);
  assert.equal(event.defaultPrevented, true); assert.equal(f.dialog(), undefined);
  field.dispatchEvent(new Event('copy'));
  assert.equal(copied.length, 1);
});

test('copy retry acknowledges API success and explicit cancellation rejects', async t => {
  const f = fixture(t), writes = [];
  clipboard(t, {writeText: async text => {
    writes.push(text);
    if (writes.length !== 2) throw new Error('Denied');
  }});
  const pending = f.host.writeClipboard('copy me'); await tick();
  f.button('Copy').click();
  assert.equal(await pending, true); assert.deepEqual(writes, ['copy me', 'copy me']);
  const cancelled = f.host.writeClipboard('retained'); await tick();
  f.button('Cancel').click();
  await assert.rejects(cancelled, /cancelled/);
  assert.equal(f.dialog(), undefined);
});

test('cancel and close own clipboard permission checks before a fallback can open', async t => {
  const f = fixture(t), methods = {};
  clipboard(t, methods);
  for (const close of [false, true]) {
    const permission = deferred();
    methods.writeText = async () => {await permission.promise; throw new Error('Denied');};
    const pending = f.host.writeClipboard('original', 92);
    if (close) {
      f.host.close();
      assert.equal(await pending, null);
      f.host.open('Reopened', 640, 400, 1); f.host.poll();
    } else {
      const rejected = assert.rejects(pending, /cancelled/);
      f.host.cancelOperation(92); await rejected;
    }
    permission.resolve(); await tick();
    assert.equal(f.dialog(), undefined);
  }
});

test('closing a manual-copy dialog removes its handler and ignores a late retry', async t => {
  const f = fixture(t), retry = deferred(); let writes = 0;
  clipboard(t, {writeText: async () => {
    if (++writes === 1) throw new Error('Denied');
    return retry.promise;
  }});
  const pending = f.host.writeClipboard('retained'); await tick();
  const field = f.dialog().children.find(node => node.tag === 'textarea');
  f.button('Copy').click();
  f.host.close();
  assert.equal(await pending, null); assert.equal(f.dialog(), undefined);
  const copied = [];
  field.dispatchEvent(Object.assign(new Event('copy'), {clipboardData: {setData: (...args) => copied.push(args)}}));
  retry.resolve(); await tick();
  assert.deepEqual(copied, []); assert.equal(f.dialog(), undefined);
});
