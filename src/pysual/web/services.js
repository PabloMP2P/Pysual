/* Browser-local dialogs only. The adapters own sessions, transport and results. */
export function createBrowserServices({
  isOpen = () => true,
  restoreFocus = () => {},
  inlineStyle = false,
} = {}) {
  let filePanel = null;
  const checkActive = signal => {
    if (signal.aborted) throw new Error("Browser operation cancelled");
    if (!isOpen()) throw new Error("The browser host is closed");
  };
  const clipboardText = text => {
    if (new TextEncoder().encode(text).length > 8 * 1024 * 1024)
      throw new Error("Clipboard text exceeds 8 MiB");
    return text;
  };

  // Buttons call browser APIs directly, retaining user activation after a
  // Python callback has yielded. Only one dialog belongs to this host at once.
  const dialog = (title, setup, signal) => new Promise((resolve, reject) => {
    try { checkActive(signal); }
    catch (error) { reject(error); return; }
    if (filePanel) {
      reject(new Error("Another browser operation is already open"));
      return;
    }
    const panel = document.createElement("dialog");
    filePanel = panel;
    if (inlineStyle) Object.assign(panel.style, {
      color: "#ecf0f8", background: "#1b2230", border: "1px solid #7289fa",
      borderRadius: "12px", padding: "24px", maxWidth: "480px", font: "16px system-ui",
    });
    const heading = document.createElement("h2");
    heading.textContent = title; panel.append(heading);
    const message = document.createElement("p"); panel.append(message);
    const actions = document.createElement("div"); actions.className = "actions";
    const cleanups = [];
    let settled = false;
    const isActive = () => !settled && !signal.aborted && isOpen();
    const finish = (value, error) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", cancelled);
      for (const cleanup of cleanups) cleanup();
      panel.close(); panel.remove(); filePanel = null;
      restoreFocus();
      error ? reject(error) : resolve(value);
    };
    const cancelled = () => finish(null, new Error("Browser operation cancelled"));
    signal.addEventListener("abort", cancelled, {once: true});
    panel.addEventListener("cancel", event => { event.preventDefault(); finish(null); });
    const button = (label, callback) => {
      const element = document.createElement("button");
      element.textContent = label;
      if (inlineStyle) element.style.margin = "6px";
      element.onclick = callback; actions.append(element);
      return element;
    };
    try {
      setup({panel, message, actions, button, finish, cleanups, isActive});
      button("Cancel", () => finish(null));
      panel.append(actions); document.body.append(panel); panel.showModal();
    } catch (error) { finish(null, error); }
  });

  const services = {
    get isDialogOpen() { return filePanel !== null; },
    dialog,
    async readClipboard(signal) {
      try {
        const text = await navigator.clipboard.readText();
        checkActive(signal);
        return clipboardText(text);
      } catch (_) {
        checkActive(signal);
        return await dialog("Read clipboard", ({panel, message, button, finish, isActive}) => {
          message.textContent = "Clipboard access was denied. Paste your text into this field, then choose Use pasted text.";
          const field = document.createElement("textarea"); field.rows = 4; panel.append(field);
          const accept = text => {
            try { finish(clipboardText(text)); }
            catch (error) { message.textContent = error.message; }
          };
          button("Read clipboard", async () => {
            if (!isActive()) return;
            try {
              const text = await navigator.clipboard.readText();
              if (isActive()) accept(text);
            } catch (error) {
              if (isActive()) {
                message.textContent = `${error.message}. Paste into the field instead.`;
                field.focus();
              }
            }
          });
          button("Use pasted text", () => { if (isActive()) accept(field.value); });
        }, signal);
      }
    },
    async writeClipboard(text, signal) {
      try {
        await navigator.clipboard.writeText(text);
        checkActive(signal);
        return true;
      } catch (_) {
        checkActive(signal);
        return await dialog("Copy to clipboard", ({panel, message, button, finish, cleanups, isActive}) => {
          message.textContent = "Clipboard access was denied. Choose Copy, or select this text and use your browser's Copy command.";
          const field = document.createElement("textarea");
          field.rows = 4; field.value = text; field.readOnly = true; panel.append(field);
          field.autofocus = true;
          field.addEventListener("focus", () => field.select(), {once: true});
          const copy = event => {
            if (!isActive() || !event.clipboardData) return;
            event.clipboardData.setData("text/plain", text);
            event.preventDefault(); finish(true);
          };
          field.addEventListener("copy", copy);
          cleanups.push(() => field.removeEventListener("copy", copy));
          button("Copy", async () => {
            if (!isActive()) return;
            try {
              await navigator.clipboard.writeText(text);
              if (isActive()) finish(true);
            } catch (_) {
              if (!isActive()) return;
              message.textContent = "Select the text and use your browser's Copy command.";
              field.focus(); field.select();
            }
          });
        }, signal);
      }
    },
    async openTextFile(limit, signal, accept = "") {
      return await dialog("Open a text file", ({panel, message, finish, isActive}) => {
        message.textContent = "Choose a UTF-8 text file from this device.";
        const picker = document.createElement("input");
        picker.type = "file"; picker.accept = accept; panel.append(picker);
        picker.oncancel = () => finish(null);
        picker.onchange = async () => {
          if (!isActive()) return;
          try {
            const file = picker.files?.[0];
            if (!file) { finish(null); return; }
            if (file.size > limit) throw new Error("File exceeds 8 MiB");
            const bytes = await file.arrayBuffer();
            if (!isActive()) return;
            const text = new TextDecoder("utf-8", {fatal: true}).decode(bytes);
            finish({name: file.name, text});
          } catch (error) { finish(null, error); }
        };
      }, signal);
    },
    async saveTextFile(text, name, signal) {
      return await dialog("Save a text file", ({panel, message, actions, button, finish, cleanups, isActive}) => {
        message.textContent = "Choose a destination for your document.";
        if (window.showSaveFilePicker) {
          let busy = false;
          const choose = button("Choose destination", async () => {
            if (busy || !isActive()) return;
            busy = true; choose.disabled = true;
            let stream = null;
            try {
              const handle = await window.showSaveFilePicker({suggestedName: name});
              if (!isActive()) return;
              stream = await handle.createWritable();
              if (!isActive()) { await stream.abort(); stream = null; return; }
              await stream.write(text);
              if (!isActive()) { await stream.abort(); stream = null; return; }
              await stream.close(); stream = null;
              if (isActive()) finish(handle.name);
            } catch (error) {
              if (stream) {
                try { await stream.abort(); }
                catch (_) { /* Preserve the original write error. */ }
              }
              if (isActive() && error.name !== "AbortError")
                message.textContent = `Save failed: ${error.message}`;
            } finally {
              busy = false;
              if (isActive()) choose.disabled = false;
            }
          });
        } else {
          message.textContent = "Download the file, then confirm that you saved it. Your document stays open if you cancel.";
          const url = URL.createObjectURL(new Blob([text], {type: "text/plain;charset=utf-8"}));
          cleanups.push(() => URL.revokeObjectURL(url));
          const link = document.createElement("a");
          link.href = url; link.download = name; link.textContent = `Download ${name}`;
          if (inlineStyle) link.style.color = "#9cb1ff";
          else link.className = "action";
          actions.append(link);
          // The Pyodide adapter converts this acknowledgement to its filename;
          // the live adapter sends the boolean through its existing protocol.
          const confirm = button("I saved the file", () => {
            if (!confirm.disabled && isActive()) finish(true);
          });
          confirm.disabled = true;
          link.onclick = event => {
            if (!isActive()) { event.preventDefault(); return; }
            confirm.disabled = false;
          };
        }
      }, signal);
    },
  };
  return services;
}
