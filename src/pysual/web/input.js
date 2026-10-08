/* Shared DOM gesture normalization; hosts own focus, transport and rendering. */
export function bindDOMInput({surface, editor, active, push, reportError, signal}) {
  const controller = new AbortController(), opts = {signal: controller.signal};
  const encoder = new TextEncoder(), maxText = 8 * 1024 * 1024;
  const aliases = {" ": "Space", Esc: "Escape"};
  const reserved = new Set(["Tab", "Backspace", "Delete", "Enter", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"]);
  let composing = false, lastComposition = null, timer;
  const commit = (text, paste = false) => {
    if (!text || !active()) return;
    if (encoder.encode(text).length <= maxText) push("text", {text, ...(paste ? {paste: true} : {})});
    else reportError("Clipboard or input text exceeds 8 MiB.");
  };
  const shortcut = event => (event.ctrlKey || event.metaKey) && !event.getModifierState?.("AltGraph");
  const nativePaste = event => active() && shortcut(event) && event.key.toLowerCase() === "v";
  for (const element of [surface, editor]) {
    element.addEventListener("keydown", event => {
      if (event.isComposing || composing || nativePaste(event)) return;
      push("key_down", {key: aliases[event.key] || event.key, shift: event.shiftKey, ctrl: shortcut(event)});
      if (reserved.has(event.key) || /^F([1-9]|1[0-2])$/.test(event.key) || shortcut(event) || (element === surface && event.key === " ")) event.preventDefault();
    }, opts);
    element.addEventListener("keyup", event => {
      if (!event.isComposing && !composing && !nativePaste(event)) push("key_up", {key: aliases[event.key] || event.key, shift: event.shiftKey, ctrl: shortcut(event)});
    }, opts);
  }
  editor.addEventListener("input", event => {
    if (event.isComposing || composing) return;
    const value = editor.value;
    editor.value = "";
    if (value !== lastComposition) commit(value, event.inputType === "insertFromPaste");
    lastComposition = null;
  }, opts);
  editor.addEventListener("compositionstart", () => { composing = true; lastComposition = null; }, opts);
  editor.addEventListener("compositionupdate", event => { if (active()) push("composition", {text: event.data}); }, opts);
  editor.addEventListener("compositionend", event => {
    composing = false; editor.value = ""; lastComposition = event.data;
    if (active()) { push("composition", {text: ""}); commit(event.data); }
    clearTimeout(timer);
    timer = setTimeout(() => { lastComposition = null; }, 0);
  }, opts);
  editor.addEventListener("paste", event => {
    if (!active() || !event.clipboardData) return;
    event.preventDefault();
    editor.value = "";
    commit(event.clipboardData.getData("text/plain"), true);
  }, opts);
  const reset = () => { composing = false; lastComposition = null; editor.value = ""; clearTimeout(timer); };
  editor.addEventListener("blur", reset, opts);
  const close = () => { controller.abort(); reset(); signal?.removeEventListener("abort", close); };
  if (signal?.aborted) close();
  else signal?.addEventListener("abort", close, {once: true});
  return close;
}
