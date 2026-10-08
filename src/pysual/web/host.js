import {createSVGClient} from "./svg.js";
/* Pyodide adapter: SVG scenes, queued input and browser services. */
export function createHost(surface) {
  let active = false, queue = [], abort = null, client = null;
  const fonts = [];
  const push = (kind, data = {}) => { if (active) queue.push({kind, ...data}); };
  const sessionOperations = new Map();
  const serviceOperations = new Map();
  let sessionNamespace = "default";
  const sessionStore = (mode, action, operation) =>
    new Promise((resolve, reject) => {
      if (!active) {
        reject(new Error("The browser host is closed"));
        return;
      }
      const token = operation ?? Symbol("session");
      if (sessionOperations.size >= 32 || sessionOperations.has(token)) {
        reject(new Error("Too many pending session operations"));
        return;
      }
      const signal = abort.signal;
      let opening,
        stopped = false,
        timer,
        cancellation;
      const releaseOperation = () => {
        if (sessionOperations.get(token) === cancellation)
          sessionOperations.delete(token);
      };
      const stopOpening = (error) => {
        stopped = true;
        action = null;
        clearTimeout(timer);
        signal.removeEventListener("abort", closed);
        releaseOperation();
        reject(error);
      };
      const closed = (error) =>
        stopOpening(
          error instanceof Error
            ? error
            : new Error("The browser host is closed"),
        );
      signal.addEventListener("abort", closed, { once: true });
      cancellation = closed;
      sessionOperations.set(token, cancellation);
      timer = setTimeout(
        () => cancellation?.(new Error("Persistent storage timed out")),
        10000,
      );
      try {
        if (!window.indexedDB)
          throw new Error("Persistent storage is unsupported");
        // Separate databases also scope the entry/byte quota. Legacy global
        // rows are deliberately not copied into every application namespace.
        opening = window.indexedDB.open(`pysual-sessions-v2:${sessionNamespace}`, 1);
      } catch (error) {
        stopOpening(error);
        return;
      }
      opening.onupgradeneeded = () => {
        if (stopped) {
          opening.transaction.abort();
          return;
        }
        if (!opening.result.objectStoreNames.contains("sessions"))
          opening.result.createObjectStore("sessions");
      };
      opening.onerror = () =>
        stopOpening(
          opening.error || new Error("Persistent storage was denied"),
        );
      opening.onblocked = () =>
        stopOpening(new Error("Persistent storage is blocked by another page"));
      opening.onsuccess = () => {
        const database = opening.result;
        signal.removeEventListener("abort", closed);
        database.onversionchange = () => database.close();
        if (stopped || signal.aborted) {
          database.close();
          stopOpening(new Error("The browser host is closed"));
          return;
        }
        let transaction,
          result = null,
          failure = null;
        try {
          transaction = database.transaction("sessions", mode);
        } catch (error) {
          database.close();
          stopOpening(error);
          return;
        }
        const shutdown = (error) => {
          if (error instanceof Error) failure = error;
          try {
            transaction.abort();
          } catch {}
        };
        cancellation = shutdown;
        sessionOperations.set(token, cancellation);
        signal.addEventListener("abort", shutdown, { once: true });
        let finished = false;
        const finish = () => {
          if (finished) return;
          finished = true;
          action = null;
          clearTimeout(timer);
          releaseOperation();
          signal.removeEventListener("abort", shutdown);
          database.close();
        };
        transaction.oncomplete = () => {
          finish();
          resolve(result);
        };
        transaction.onabort = transaction.onerror = () => {
          finish();
          reject(
            failure ||
              transaction.error ||
              new Error("Persistent storage failed"),
          );
        };
        try {
          action(
            transaction.objectStore("sessions"),
            (value) => {
              result = value;
            },
            (error) => {
              failure = error;
              transaction.abort();
            },
          );
        } catch (error) {
          failure = error;
          transaction.abort();
        }
      };
    });
  const validateSession = (key, text) => {
    if (typeof key !== "string" || !/^[A-Za-z0-9_.-]{1,128}$/.test(key))
      throw new Error("Invalid session key");
    if (
      text !== null &&
      (typeof text !== "string" ||
        new TextEncoder().encode(text).length > 8 * 1024 * 1024)
    )
      throw new Error("Sessions must be UTF-8 text of at most 8 MiB");
  };

  const host = {
    width: 960, height: 640, frames: 0, renderScale: 1, resourceRevision: 0,
    viewport: JSON.stringify({width: 960, height: 640, scale: 1}),
    async loadFonts(ui, mono) {
      const loaded = await Promise.all([
        new FontFace("Pysual Sans", ui).load(), new FontFace("Pysual Mono", mono).load(),
      ]);
      for (const font of fonts) document.fonts.delete(font);
      fonts.splice(0, fonts.length, ...loaded);
      for (const font of fonts) document.fonts.add(font);
      host.resourceRevision++;
    },
    open(title, width, height, scale) {
      if (active) throw new Error("A browser host session is already active");
      active = true; queue = []; host.frames = 0;
      abort = new AbortController();
      try {
        for (const font of fonts) document.fonts.add(font);
        surface.replaceChildren(); document.title = title;
        client = createSVGClient(surface, {push, signal: abort.signal,
          reportError: text => push("resource_error", {text}),
          onViewport: value => {
            if (host.width !== value.width || host.height !== value.height || host.renderScale !== value.scale) host.resourceRevision++;
            host.width = value.width; host.height = value.height; host.renderScale = value.scale;
            host.viewport = JSON.stringify(value);
          },
        });
      } catch (error) {
        host.close();
        throw error;
      }
    },
    title(text) {
      document.title = text; surface.setAttribute("aria-label", text);
    },
    poll() { const data = JSON.stringify(queue); queue = []; return data; },
    present(packet) {
      if (!active) return;
      client.apply(JSON.parse(packet)); host.frames++;
    },
    close() {
      active = false; abort?.abort(); client?.close(); client = null; queue = [];
      for (const font of fonts) document.fonts.delete(font);
    },
    textInput(x, y, w, h) { client?.focusInput(w < 0 ? null : [x, y, w, h]); },
    async readClipboard(signal) {
      const text = await client.service({method: "clipboard_read"}, signal);
      if (text === null) throw new Error("Clipboard read cancelled");
      return text;
    },
    async writeClipboard(text, signal) {
      const copied = await client.service({method: "clipboard_write", text}, signal);
      if (copied !== true) throw new Error("Clipboard write cancelled");
      return true;
    },
    cancelSession(operation) {
      sessionOperations.get(operation)?.();
    },
    cancelOperation(operation) {
      serviceOperations.get(operation)?.abort();
    },
    setSessionNamespace(namespace) {
      if (typeof namespace !== "string" || !namespace || namespace.length > 1024)
        throw new Error("Invalid session namespace");
      sessionNamespace = namespace;
    },
    async readSessionJSON(key, operation) {
      return JSON.stringify(await host.readSession(key, operation));
    },
    async writeSessionJSON(key, text, operation) {
      return await host.writeSession(key, JSON.parse(text), operation);
    },
    async readSession(key, operation) {
      validateSession(key, null);
      return await sessionStore(
        "readonly",
        (store, done, fail) => {
          const request = store.get(key);
          request.onsuccess = () => {
            try {
              const value = request.result?.text ?? null;
              validateSession(key, value);
              done(value);
            } catch (error) {
              fail(error);
            }
          };
        },
        operation,
      );
    },
    async writeSession(key, text, operation) {
      validateSession(key, text);
      return await sessionStore(
        "readwrite",
        (store, done, fail) => {
          if (text === null) {
            store.delete(key);
            return;
          }
          const bytes = new TextEncoder().encode(text).length;
          let total = bytes,
            count = 1;
          const cursor = store.openCursor();
          cursor.onsuccess = () => {
            const row = cursor.result;
            if (row) {
              if (row.key !== key) {
                count++;
                total +=
                  row.value.bytes ||
                  new TextEncoder().encode(row.value.text).length;
              }
              row.continue();
              return;
            }
            if (count > 32 || total > 32 * 1024 * 1024) {
              fail(new Error("Session storage exceeds 32 entries or 32 MiB"));
              return;
            }
            store.put({ text, bytes }, key);
          };
        },
        operation,
      );
    },

    async openUrl(url, signal) {
      return await client.service({method: "open_url", url}, signal);
    },
    async openTextFile(limit, signal) {
      return JSON.stringify(await client.service({method: "open_text_file", limit}, signal));
    },
    async saveTextFile(text, name, signal) {
      const saved = await client.service({method: "save_text_file", text, name}, signal);
      return saved === true ? name : saved;
    },
    requestClose() { push("close"); },
  };
  // Reserve before any asynchronous clipboard permission check: cancelling
  // there must also prevent a fallback dialog from appearing afterward.
  for (const [name, arity] of [["readClipboard", 0], ["writeClipboard", 1], ["openUrl", 1], ["openTextFile", 1], ["saveTextFile", 2]]) {
    const service = host[name];
    host[name] = async (...args) => {
      const operation = args.length > arity ? args.pop() : Symbol(name);
      if (!active) throw new Error("The browser host is closed");
      if (serviceOperations.size >= 32 || serviceOperations.has(operation))
        throw new Error("Too many pending browser operations");
      const controller = new AbortController(), signal = controller.signal;
      const sessionSignal = abort.signal, close = () => controller.abort();
      serviceOperations.set(operation, controller);
      sessionSignal.addEventListener("abort", close, {once: true});
      let cancel;
      const cancelled = new Promise((resolve, reject) => {
        cancel = () => sessionSignal.aborted ? resolve(null) : reject(new Error("Browser operation cancelled"));
        signal.addEventListener("abort", cancel, {once: true});
      });
      try {
        return await Promise.race([service(...args, signal), cancelled]);
      } finally {
        sessionSignal.removeEventListener("abort", close);
        signal.removeEventListener("abort", cancel);
        if (serviceOperations.get(operation) === controller) serviceOperations.delete(operation);
      }
    };
  }
  return host;
}
