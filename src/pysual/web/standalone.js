/* One-file bootstrap. Runtime bytes are embedded; ordinary app I/O stays ordinary. */
const status = document.querySelector('#status');
const bundle = JSON.parse(document.querySelector('#pysual-bundle').textContent);
const bytes = name => {
  if (!(name in bundle.files)) throw new Error('Missing embedded file: ' + name);
  const encoded = bundle.files[name];
  if (typeof Uint8Array.fromBase64 === 'function') return Uint8Array.fromBase64(encoded);
  // Build output once; aligned chunks keep temporary strings bounded on older browsers.
  const padding = encoded.endsWith('==') ? 2 : encoded.endsWith('=') ? 1 : 0;
  const result = new Uint8Array(encoded.length / 4 * 3 - padding);
  let offset = 0;
  for (let start = 0; start < encoded.length; start += 65536) {
    const raw = atob(encoded.slice(start, start + 65536));
    for (let i = 0; i < raw.length; i++) result[offset++] = raw.charCodeAt(i);
  }
  return result;
};
const urls = new Map();
const url = (name, type) => {
  if (!urls.has(name)) urls.set(name, URL.createObjectURL(new Blob([bytes(name)], {type})));
  return urls.get(name);
};
window.addEventListener('pagehide', () => {
  for (const value of urls.values()) URL.revokeObjectURL(value);
  urls.clear();
}, {once: true});

try {
  // Relative imports cannot resolve from a blob URL. Bind the shipped helper
  // modules to their embedded URLs before creating the host module.
  const svgSource = new TextDecoder().decode(bytes('svg.js'))
    .replace('"./input.js"', JSON.stringify(url('input.js', 'text/javascript')))
    .replace('"./services.js"', JSON.stringify(url('services.js', 'text/javascript')));
  urls.set('svg.js', URL.createObjectURL(new Blob([svgSource], {type: 'text/javascript'})));
  const hostSource = new TextDecoder().decode(bytes('host.js'))
    .replace('"./svg.js"', JSON.stringify(url('svg.js', 'text/javascript')));
  urls.set('host.js', URL.createObjectURL(new Blob([hostSource], {type: 'text/javascript'})));
  const {createHost} = await import(url('host.js', 'text/javascript'));
  window.pysualHost = createHost(document.querySelector('#app'));
  // Emscripten publishes the factory used by loadPyodide; load it before the
  // bootstrap's dynamic script request. No application source is rewritten.
  await import(url('pyodide/pyodide.asm.js', 'text/javascript'));
  const {loadPyodide} = await import(url('pyodide/pyodide.mjs', 'text/javascript'));
  const prefix = 'https://pysual-embedded.invalid/';
  const originalFetch = window.fetch;
  window.fetch = (input, options) => {
    const address = typeof input === 'string' ? input : input.url || String(input);
    if (address.startsWith(prefix)) {
      const path = address.slice(prefix.length);
      try {
        return Promise.resolve(new Response(bytes('pyodide/' + path), {headers: {
          'Content-Type': path.endsWith('.wasm') ? 'application/wasm' : 'application/octet-stream'
        }}));
      } catch (error) { return Promise.reject(error); }
    }
    return originalFetch.call(window, input, options);
  };
  let pyodide;
  try {
    pyodide = await loadPyodide({
      indexURL: prefix,
      stdLibURL: prefix + 'python_stdlib.zip',
      lockFileContents: new TextDecoder().decode(bytes('pyodide/pyodide-lock.json')),
      packageBaseUrl: prefix
    });
  } finally { window.fetch = originalFetch; }
  window.pyodide = pyodide;
  pyodide.FS.mkdirTree('/app');
  pyodide.FS.chdir('/app');
  pyodide.unpackArchive(bytes('pysual.zip'), 'zip', {extractDir: '/app'});
  await window.pysualHost.loadFonts(
    pyodide.FS.readFile('pysual/assets/DejaVuSans.ttf').buffer,
    pyodide.FS.readFile('pysual/assets/DejaVuSansMono.ttf').buffer
  );
  const source = new TextDecoder().decode(bytes('app.py'));
  pyodide.FS.writeFile('app.py', source);
  pyodide.globals.set('__file__', 'app.py');
  pyodide.globals.set('__name__', '__main__');
  pyodide.globals.set('__pysual_source', source);
  status.textContent = '';
  window.pysualRunning = true;
  await pyodide.runPythonAsync("import sys as __pysual_sys\n__pysual_sys.path.insert(0, '/app')\nfrom pyodide.code import eval_code_async as __pysual_eval\nfrom pysual.backends import backend_override as __pysual_backend_override\n__pysual_sys.argv[:1] = ['app.py']\nwith __pysual_backend_override('web'):\n    await __pysual_eval(__pysual_source, globals=globals(), filename='app.py')");
  window.pysualRunning = false;
  window.pysualFinished = true;
} catch (error) {
  window.pysualRunning = false;
  window.pysualFinished = true;
  window.pysualError = String(error);
  status.textContent = 'Unable to start Pysual: ' + error;
  console.error(error);
}
