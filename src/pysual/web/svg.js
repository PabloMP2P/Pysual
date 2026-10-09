import {bindDOMInput} from "./input.js";
import {createBrowserServices} from "./services.js";

/* The shared browser surface. Python owns scene contents and application state. */
const NS = "http://www.w3.org/2000/svg";
// This module owns the rendered nodes. Retain their last applied values instead
// of reading live DOM attributes/text for every unchanged descendant.
const rendered = new WeakMap();
export function reconcile(element, node, imageSources) {
  const oldImage = element && rendered.get(element)?.attrs["data-pysual-image"];
  // Reloads get a new resource identity even when their URL is unchanged.
  // A fresh image retries decoding and detaches pending events from the old one.
  if (!element || element.localName !== node.tag || (node.tag === "image"
      && oldImage !== node.attrs["data-pysual-image"])) {
    const replacement = document.createElementNS(NS, node.tag);
    if (element) element.replaceWith(replacement);
    element = replacement;
  }
  let previous = rendered.get(element);
  if (!previous) {
    previous = {attrs: Object.fromEntries(Array.from(element.attributes, attr => [attr.name, attr.value]))};
    if (node.text === undefined) {
      for (const child of Array.from(element.childNodes)) if (child.nodeType === 3) child.remove();
    }
  }
  // Transport references expand to ordinary SVG image hrefs. This preserves
  // browser image sizing, fit, decode errors and clipping without <use> rules.
  const attrs = node.image === undefined ? node.attrs
    : {...node.attrs, href: imageSources.get(node.image)};
  for (const name of Object.keys(previous.attrs)) {
    if (!(name in attrs)) element.removeAttribute(name);
  }
  for (const [name, value] of Object.entries(attrs)) {
    if (previous.attrs[name] !== value) element.setAttribute(name, value);
  }
  if (node.text !== undefined) {
    if (previous.text !== node.text) element.textContent = node.text;
  } else {
    if (previous.text !== undefined) element.textContent = "";
    const children = node.children || [];
    for (let index = 0; index < children.length; index++) {
      const child = reconcile(element.children[index], children[index], imageSources);
      if (!child.parentNode) element.appendChild(child);
    }
    while (element.children.length > children.length) element.lastElementChild.remove();
  }
  rendered.set(element, {attrs: {...attrs}, text: node.text});
  return element;
}

export function pointerEvent(surface, event) {
  const box = surface.getBoundingClientRect();
  return {x: event.clientX - box.left, y: event.clientY - box.top, button: event.button + 1,
    pointer_id: event.pointerId, pointer_kind: event.pointerType || "mouse",
    shift: event.shiftKey, ctrl: event.ctrlKey || event.metaKey};
}

export function viewportSnapshot(surface, safe, textActive, bounds = surface.getBoundingClientRect()) {
  const css = window.getComputedStyle?.(safe);
  const vv = window.visualViewport;
  return {width: bounds.width, height: bounds.height, scale: window.devicePixelRatio || 1,
    safe_area: ["paddingTop", "paddingRight", "paddingBottom", "paddingLeft"].map(key => Math.max(0, parseFloat(css?.[key]) || 0)),
    keyboard_occlusion: textActive && vv && Math.abs((vv.scale || 1) - 1) < 0.01
      ? Math.max(0, Math.min(bounds.height, bounds.top + bounds.height - vv.height - vv.offsetTop)) : 0};
}

export async function browserOperation(browserServices, command, signal) {
  switch (command.method) {
    case "clipboard_read": return await browserServices.readClipboard(signal);
    case "clipboard_write": return await browserServices.writeClipboard(command.text, signal);
    case "open_text_file": return await browserServices.openTextFile(
      command.limit ?? 8 * 1024 * 1024, signal, ".txt,.json,.py,.md,.csv,.svg,.html,text/*,application/json");
    case "save_text_file": return await browserServices.saveTextFile(command.text, command.name, signal);
    case "open_url": {
      if (!/^https?:\/\//i.test(command.url) && !/^mailto:/i.test(command.url)) throw new Error("Unsupported URL scheme");
      return await browserServices.dialog("Open link", ({message, button, finish}) => {
        message.textContent = command.url;
        button("Open in browser", () => {
          try {
            const opened = window.open(command.url, "_blank");
            if (!opened) throw new Error("The browser blocked this link");
            opened.opener = null;
            finish(true);
          } catch (error) { message.textContent = error.message; }
        });
      }, signal);
    }
    default: throw new Error("Unsupported browser operation");
  }
}

export function createSVGClient(surface, {push, reportError = () => {}, onViewport = () => {}, editor, safe, signal} = {}) {
  let active = true, textActive = false, lastViewport = "", lastTitle, lastViewBox;
  let surfaceBounds, textRect = null, editorGeometry = "";
  let definitionBank = null, observer = null;
  const definitions = new Map(), imageSources = new Map();
  const ownEditor = !editor, ownSafe = !safe;
  editor ??= document.createElement("textarea");
  safe ??= document.createElement("div");
  const controller = new AbortController(), opts = {signal: controller.signal};
  function close() {
    if (!active) return;
    active = false; textActive = false; controller.abort(); observer?.disconnect();
    signal?.removeEventListener("abort", close);
    definitions.clear(); imageSources.clear(); definitionBank = null;
    editor.value = ""; editor.blur();
    if (ownEditor) editor.remove();
    if (ownSafe) safe.remove();
  }
  // Register before attaching DOM or listeners, so host.open can unwind a
  // partially constructed client if browser initialization throws.
  signal?.addEventListener("abort", close, {once: true});
  editor.autocomplete = "off"; editor.autocapitalize = "off"; editor.spellcheck = false;
  editor.setAttribute("aria-label", "Application text input");
  Object.assign(editor.style, {position: "fixed", opacity: ".01", resize: "none", border: "0", padding: "0", margin: "0",
    outline: "0", pointerEvents: "none", zIndex: "-1", font: "16px 'Pysual Sans'", width: "1px", height: "1px"});
  Object.assign(safe.style, {position: "fixed", visibility: "hidden", pointerEvents: "none",
    padding: "env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)"});
  if (ownEditor) document.body.append(editor);
  if (ownSafe) document.body.append(safe);
  surface.tabIndex = 0;
  surface.setAttribute("role", "application");
  Object.assign(surface.style, {display: "block", width: "100%", height: "100%", outline: "0", touchAction: "none", userSelect: "none"});
  const restoreFocus = () => { if (active) (textActive ? editor : surface).focus({preventScroll: true}); };
  const browserServices = createBrowserServices({isOpen: () => active, restoreFocus, inlineStyle: true});
  const emit = (kind, data = {}) => { if (active) push(kind, data); };
  function viewport() {
    if (!active) return;
    surfaceBounds = surface.getBoundingClientRect();
    positionEditor();
    const value = viewportSnapshot(surface, safe, textActive, surfaceBounds), encoded = JSON.stringify(value);
    if (encoded !== lastViewport) { lastViewport = encoded; onViewport(value); emit("viewport", {viewport: value}); }
  }
  function positionEditor() {
    if (!textRect) return;
    surfaceBounds ??= surface.getBoundingClientRect();
    const geometry = [surfaceBounds.left + textRect[0], surfaceBounds.top + textRect[1], Math.max(1, textRect[2]), Math.max(1, textRect[3])];
    const encoded = geometry.join(",");
    if (encoded === editorGeometry) return;
    editorGeometry = encoded;
    Object.assign(editor.style, {left: `${geometry[0]}px`, top: `${geometry[1]}px`, width: `${geometry[2]}px`, height: `${geometry[3]}px`});
  }
  function focusInput(rect) {
    if (!active) return;
    const changed = textActive !== (rect !== null);
    textActive = rect !== null;
    textRect = rect;
    if (rect) {
      positionEditor();
      if (!browserServices.isDialogOpen && (document.hasFocus?.() ?? true) && document.activeElement !== editor) editor.focus({preventScroll: true});
    } else {
      if (editor.value) editor.value = "";
      if (!browserServices.isDialogOpen && document.activeElement === editor) surface.focus({preventScroll: true});
    }
    if (changed) viewport();
  }
  function imageFailed(event) {
    const element = event.target;
    if (element.localName !== "image") return;
    const identifier = element.getAttribute("data-pysual-image");
    if (identifier) emit("image_error", {text: identifier});
  }
  surface.addEventListener("error", imageFailed, {...opts, capture: true});
  surface.addEventListener("pointerdown", event => {
    event.preventDefault(); restoreFocus(); surface.setPointerCapture(event.pointerId);
    emit("pointer_down", pointerEvent(surface, event));
  }, opts);
  surface.addEventListener("pointermove", event => emit("pointer_move", {...pointerEvent(surface, event), button: 1}), opts);
  surface.addEventListener("pointerup", event => {
    emit("pointer_up", pointerEvent(surface, event));
    if (surface.hasPointerCapture(event.pointerId)) surface.releasePointerCapture(event.pointerId);
  }, opts);
  for (const [name, kind] of [["pointercancel", "pointer_cancel"], ["pointerleave", "pointer_leave"]]) {
    surface.addEventListener(name, event => emit(kind, pointerEvent(surface, event)), opts);
  }
  surface.addEventListener("contextmenu", event => event.preventDefault(), opts);
  surface.addEventListener("wheel", event => {
    event.preventDefault();
    const horizontal = Math.abs(event.deltaX) > Math.abs(event.deltaY);
    const delta = horizontal ? event.deltaX : event.deltaY;
    if (!delta) return;
    const bounds = surface.getBoundingClientRect();
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2
      ? (horizontal ? bounds.width : bounds.height) : 1;
    emit("wheel", {x: event.clientX - bounds.left, y: event.clientY - bounds.top,
      delta: delta * unit / 100,
      shift: horizontal || event.shiftKey, ctrl: event.ctrlKey || event.metaKey});
  }, {...opts, passive: false});
  bindDOMInput({surface, editor, active: () => active && textActive, push: emit, reportError, signal: controller.signal});
  window.addEventListener("blur", () => { textActive = false; textRect = null; editor.value = ""; emit("blur"); viewport(); }, opts);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { emit("blur"); emit("suspend"); }
    else { viewport(); emit("resume"); }
  }, opts);
  window.addEventListener("pagehide", () => emit("suspend"), opts);
  window.addEventListener("pageshow", () => { viewport(); emit("resume"); }, opts);
  window.addEventListener("resize", viewport, opts);
  window.addEventListener("scroll", viewport, {...opts, capture: true});
  window.visualViewport?.addEventListener("resize", viewport, opts);
  window.visualViewport?.addEventListener("scroll", viewport, opts);
  observer = new ResizeObserver(viewport); observer.observe(surface);
  const trackDpr = () => {
    if (!active) return;
    window.matchMedia?.(`(resolution: ${window.devicePixelRatio || 1}dppx)`).addEventListener("change", () => {
      viewport(); trackDpr();
    }, {...opts, once: true});
  };
  trackDpr();
  surface.focus(); viewport();
  return {
    viewport, focusInput,
    service: (command, signal) => browserOperation(browserServices, command, signal),
    apply(packet) {
      if (!active) return;
      if (packet.reset) imageSources.clear();
      for (const [id, source] of Object.entries(packet.image_sources || {})) imageSources.set(id, source);
      const incoming = packet.definitions || [];
      const obsolete = new Set(packet.remove_definitions || []);
      if (packet.reset) {
        const retained = new Set(incoming.map(node => node.attrs.id));
        for (const id of definitions.keys()) if (!retained.has(id)) obsolete.add(id);
      }
      // Resource updates are keyed independently of body positions, so opening
      // one popup does not rewrite every unchanged gradient in the application.
      for (const node of incoming) {
        if (!definitionBank) {
          definitionBank = document.createElementNS(NS, "defs");
          surface.appendChild(definitionBank);
        }
        const element = reconcile(definitions.get(node.attrs.id), node, imageSources);
        if (!element.parentNode) definitionBank.appendChild(element);
        definitions.set(node.attrs.id, element);
      }
      // A reset supplies every top-level node after transport history expires.
      // Reconcile that full snapshot too: clearing here rebuilds the whole SVG
      // whenever a busy native producer outruns the browser's last revision.
      const bodies = Array.from(surface.children).filter(child => child !== definitionBank);
      for (const [index, node] of packet.updates) {
        const child = reconcile(bodies[index], node, imageSources);
        if (!child.parentNode) surface.appendChild(child);
      }
      for (let index = packet.length; index < bodies.length; index++) bodies[index].remove();
      // Keep old resources available until all body references have changed.
      for (const id of obsolete) { definitions.get(id)?.remove(); definitions.delete(id); }
      for (const id of packet.remove_images || []) imageSources.delete(id);
      if (!definitions.size) { definitionBank?.remove(); definitionBank = null; }
      const viewBox = `0 0 ${packet.width} ${packet.height}`;
      if (viewBox !== lastViewBox) { surface.setAttribute("viewBox", viewBox); lastViewBox = viewBox; }
      if (packet.title !== lastTitle) { document.title = packet.title; surface.setAttribute("aria-label", packet.title); lastTitle = packet.title; }
      focusInput(packet.text_input);
    },
    close,
  };
}
