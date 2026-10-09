// Shared browser-local contracts, independent of Canvas/SVG transport wrappers.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {dirname, join} from 'node:path';
import test from 'node:test';

const source = readFileSync(join(dirname(process.env.PYSUAL_TEST_HOST), 'services.js'), 'utf8');
const {createBrowserServices} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((done, fail) => {resolve = done; reject = fail;});
  return {promise, resolve, reject};
};

function fixture(t) {
  class Element extends EventTarget {
    constructor(tag) { super(); this.tag = tag; this.children = []; this.style = {}; }
    append(child) { child.parent = this; this.children.push(child); }
    remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
    showModal() {}
    close() {}
    focus() {}
    select() {}
  }
  const document = {body: new Element('body'), createElement: tag => new Element(tag)};
  for (const [name, value] of Object.entries({document, window: {}, navigator: {}})) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, {configurable: true, value});
    t.after(() => previous ? Object.defineProperty(globalThis, name, previous) : delete globalThis[name]);
  }
  let focusRestores = 0;
  const services = createBrowserServices({restoreFocus: () => {focusRestores++;}});
  const input = () => document.body.children.at(-1).children.find(node => node.tag === 'input');
  return {services, input, get focusRestores() {return focusRestores;}};
}

test('file import validates bytes before reading and rejects invalid UTF-8 without retaining a dialog', async t => {
  const f = fixture(t), signal = new AbortController().signal;
  let reads = 0;
  const oversized = f.services.openTextFile(4, signal);
  const rejectedSize = assert.rejects(oversized, /8 MiB/);
  f.input().files = [{name: 'large.txt', size: 5, arrayBuffer: async () => {reads++;}}];
  await f.input().onchange(); await rejectedSize;
  assert.equal(reads, 0); assert.equal(f.services.isDialogOpen, false);

  const invalid = f.services.openTextFile(4, signal);
  const rejectedEncoding = assert.rejects(invalid, /encoded data|encoding/i);
  f.input().files = [{name: 'invalid.txt', size: 1, arrayBuffer: async () => new Uint8Array([255]).buffer}];
  await f.input().onchange(); await rejectedEncoding;
  assert.equal(f.services.isDialogOpen, false);

  const valid = f.services.openTextFile(4, signal);
  f.input().files = [{name: 'café.txt', size: 4, arrayBuffer: async () => new TextEncoder().encode('éé').buffer}];
  await f.input().onchange();
  assert.deepEqual(await valid, {name: 'café.txt', text: 'éé'});
  assert.equal(f.focusRestores, 3);
});

test('cancelled slow import cannot dismiss the next dialog or restore its focus twice', async t => {
  const f = fixture(t), reading = deferred(), old = new AbortController();
  const first = f.services.openTextFile(100, old.signal);
  const cancelled = assert.rejects(first, /cancelled/);
  f.input().files = [{name: 'old.txt', size: 3, arrayBuffer: () => reading.promise}];
  const oldChange = f.input().onchange();
  old.abort(); await cancelled;
  assert.equal(f.focusRestores, 1); assert.equal(f.services.isDialogOpen, false);

  const next = f.services.openTextFile(100, new AbortController().signal);
  const nextInput = f.input();
  reading.resolve(new TextEncoder().encode('old').buffer); await oldChange;
  assert.equal(f.services.isDialogOpen, true);
  assert.equal(f.input(), nextInput); assert.equal(f.focusRestores, 1);
  nextInput.oncancel(); assert.equal(await next, null);
  assert.equal(f.services.isDialogOpen, false); assert.equal(f.focusRestores, 2);
});

test('dialog reservations belong to each controller and abort releases only its own panel', async t => {
  const f = fixture(t), other = createBrowserServices(), controller = new AbortController();
  const first = f.services.openTextFile(100, controller.signal);
  const cancelled = assert.rejects(first, /cancelled/);
  await assert.rejects(f.services.openTextFile(100, controller.signal), /already open/);
  const second = other.openTextFile(100, new AbortController().signal);
  const secondInput = f.input();
  controller.abort(); await cancelled;
  assert.equal(f.services.isDialogOpen, false); assert.equal(other.isDialogOpen, true);
  assert.equal(f.input(), secondInput);
  secondInput.oncancel(); assert.equal(await second, null);
  assert.equal(other.isDialogOpen, false);
});

for (const cancellation of ['button', 'escape', 'signal']) {
  test(`save reports committed output when ${cancellation} cancellation arrives during close`, async t => {
    const f = fixture(t), closing = deferred(), started = deferred(), controller = new AbortController();
    let aborts = 0, settled = false;
    window.showSaveFilePicker = async () => ({name: 'saved.txt', createWritable: async () => ({
      write: async () => {},
      close: () => { started.resolve(); return closing.promise; },
      abort: async () => { aborts++; },
    })});
    const result = f.services.saveTextFile('snapshot', 'saved.txt', controller.signal);
    result.finally(() => { settled = true; });
    const panel = document.body.children[0], actions = panel.children.at(-1).children;
    const choosing = actions.find(button => button.textContent === 'Choose destination').onclick();
    await started.promise;
    const cancel = actions.find(button => button.textContent === 'Cancel');
    assert.equal(cancel.disabled, true);
    if (cancellation === 'button') cancel.onclick();
    if (cancellation === 'escape') {
      const event = new Event('cancel', {cancelable: true});
      panel.dispatchEvent(event);
      assert.equal(event.defaultPrevented, true);
    }
    if (cancellation === 'signal') controller.abort();
    await Promise.resolve();
    assert.equal(settled, false);
    assert.equal(f.services.isDialogOpen, true);
    await assert.rejects(f.services.openTextFile(100, new AbortController().signal), /already open/);
    closing.resolve(); await choosing;
    assert.equal(await result, 'saved.txt');
    assert.equal(aborts, 0);
    assert.equal(f.services.isDialogOpen, false);
    assert.equal(f.focusRestores, 1);
  });
}

test('failed close reports the commit error even after cancellation and releases the dialog', async t => {
  const f = fixture(t), closing = deferred(), started = deferred(), controller = new AbortController();
  let aborts = 0;
  window.showSaveFilePicker = async () => ({name: 'saved.txt', createWritable: async () => ({
    write: async () => {},
    close: () => { started.resolve(); return closing.promise; },
    abort: async () => { aborts++; },
  })});
  const result = f.services.saveTextFile('snapshot', 'saved.txt', controller.signal);
  const rejected = assert.rejects(result, /Disk full/);
  const choosing = document.body.children[0].children.at(-1).children[0].onclick();
  await started.promise;
  controller.abort(); closing.reject(new Error('Disk full'));
  await choosing; await rejected;
  assert.equal(aborts, 1);
  assert.equal(f.services.isDialogOpen, false);
  assert.equal(f.focusRestores, 1);
});
