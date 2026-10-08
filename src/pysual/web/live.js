import {createSVGClient} from "./svg.js";
/* SVG and browser services only. Python owns all UI state and painting. */
(() => {
  "use strict";
  const svg = document.getElementById("frame"), editor = document.getElementById("editor");
  const status = document.getElementById("status"), safe = document.getElementById("safe");
  const token = location.hash.slice(1);
  const MAX_TEXT = 8 * 1024 * 1024, MAX_REQUEST = MAX_TEXT * 6 + 65536;
  const encoder = new TextEncoder();
  const headers = {"X-Pysual-Token": token, "Content-Type": "application/json"};
  let active = true, revision = -1, sending = false, events = [], replies = [];
  let connected = false, disconnectedAt = null, disconnectTimer = null, departing = false;
  const operations = new Map(), complete = new Set();
  const connection = new AbortController();
  const client = createSVGClient(svg, {push, reportError: announce, editor, safe});

  function announce(text) { status.textContent = text; status.hidden = !text; }
  function push(kind, data = {}) {
    if (!active) return;
    const event = {kind, ...data};
    if (["pointer_move", "viewport"].includes(kind) && events.at(-1)?.kind === kind) events[events.length - 1] = event;
    else events.push(event);
    if (events.length > 1024) events = events.filter(event => event.kind !== "pointer_move");
    queueMicrotask(send);
  }
  async function send() {
    if (sending || !active || (!events.length && !replies.length)) return;
    sending = true;
    // One maximum-size result fits even when every byte needs JSON escaping.
    const batch = events.splice(0, 256), answers = replies.splice(0, 1);
    let body = JSON.stringify({events: batch, replies: answers});
    while (encoder.encode(body).length > MAX_REQUEST && batch.length) {
      events.unshift(batch.pop());
      body = JSON.stringify({events: batch, replies: answers});
    }
    if (encoder.encode(body).length > MAX_REQUEST) {
      answers[0] = {id: answers[0].id, error: "Browser result exceeds the encoded transport limit"};
      body = JSON.stringify({events: batch, replies: answers});
    }
    try {
      const response = await fetch("/events", {method: "POST", headers, body,
        signal: AbortSignal.any([connection.signal, AbortSignal.timeout(20000)])});
      if (!active) return;
      if (response.status === 403) {
        stop("This tab has no valid application connection. Reopen the URL printed by Python.");
        return;
      }
      if ([400, 413, 415].includes(response.status)) {
        const message = `Browser input rejected (${response.status}); the operation was not applied.`;
        announce(message);
        // The original invalid payload is never retried. Complete pending Python
        // operations once with a small, valid error reply instead.
        if (answers.length) {
          const rejected = answers.splice(0);
          const failure = await fetch("/events", {method: "POST", headers,
            body: JSON.stringify({replies: rejected.map(answer => ({id: answer.id, error: message}))}),
            signal: AbortSignal.any([connection.signal, AbortSignal.timeout(20000)])});
          if (!failure.ok) stop(message);
        }
        return;
      }
      if (!response.ok) throw new Error(`Input request failed (${response.status})`);
    } catch (error) {
      if (!active) return;
      // Do not replay pointer/key actions after an uncertain response. Replies
      // are idempotent and can safely be redelivered on the next attempt.
      replies.unshift(...answers);
      events.unshift({kind: "blur"});
      announce("Connection interrupted. Reconnecting…");
      await new Promise(resolve => setTimeout(resolve, 500));
    } finally {
      sending = false;
      if (active && (events.length || replies.length)) queueMicrotask(send);
    }
  }
  function apply(packet) {
    client.apply(packet);
    revision = packet.revision;
    const alive = new Set(packet.commands.map(command => command.id));
    for (const [id, cancel] of operations) if (!alive.has(id)) { cancel(); operations.delete(id); }
    for (const command of packet.commands) if (!operations.has(command.id) && !complete.has(command.id)) runOperation(command);
    if (packet.closed) stop("Application closed. You can close this tab.");
  }
  function receiveFrame(packet) {
    connected = true;
    disconnectedAt = null;
    clearTimeout(disconnectTimer); disconnectTimer = null;
    announce("");
    apply(packet);
  }
  function connectionLost() {
    disconnectedAt ??= Date.now();
    announce(connected ? "Python disconnected. Reconnecting…" : "Waiting for the Python application…");
    if (connected && disconnectTimer === null) {
      // EventSource can stay CONNECTING without another error before this deadline.
      disconnectTimer = setTimeout(() => {
        if (active && disconnectedAt !== null) stop("Python connection ended. You can close this tab.");
      }, Math.max(0, 8000 - (Date.now() - disconnectedAt)));
    }
  }
  async function frames() {
    while (active) {
      try {
        const response = await fetch(`/frame?revision=${revision}`, {headers,
          signal: AbortSignal.any([connection.signal, AbortSignal.timeout(22000)])});
        if (!active) break;
        if (!response.ok) {
          if (response.status === 403) { stop("This tab has no valid application connection. Reopen the URL printed by Python."); break; }
          throw new Error(`Frame request failed (${response.status})`);
        }
        const packet = await response.json();
        if (!active) break;
        receiveFrame(packet);
      } catch (error) {
        if (!active) break;
        connectionLost();
        revision = -1;
        await new Promise(resolve => setTimeout(resolve, 750));
      }
    }
  }
  function startFrames() {
    if (typeof EventSource !== "function") { frames(); return; }
    let source;
    try {
      source = new EventSource(`/stream?token=${encodeURIComponent(token)}`);
    } catch (error) {
      frames();
      return;
    }
    let delivered = false;
    source.onmessage = (event) => {
      if (!active) return;
      delivered = true;
      receiveFrame(JSON.parse(event.data));
    };
    source.onerror = () => {
      if (!active) return;
      if (source.readyState === EventSource.CLOSED) {
        source.close();
        if (!delivered) frames();
        else stop("Python connection ended. You can close this tab.");
      } else connectionLost();
    };
    connection.signal.addEventListener("abort", () => source.close());
  }
  function disconnect() {
    if (departing) return;
    departing = true;
    fetch("/events", {method: "POST", headers, body: JSON.stringify({disconnect: true, events: [{kind: "blur"}]}), keepalive: true}).catch(() => {});
  }
  // Some browser navigation paths omit pagehide; either event can start grace.
  for (const name of ["beforeunload", "pagehide"]) window.addEventListener(name, disconnect, {signal: connection.signal});
  document.getElementById("quit").addEventListener("click", async () => {
    push("close");
    document.getElementById("host-menu").open = false;
    announce("Closing application…");
  }, {signal: connection.signal});
  for (const format of ["html", "svg"]) document.getElementById(`${format}-export`).addEventListener("click", async () => {
    try {
      const response = await fetch(`/export.${format}`, {headers, signal: connection.signal});
      if (!response.ok) throw new Error("Snapshot export failed");
      const content = await response.blob();
      if (!active) return;
      const url = URL.createObjectURL(content), link = document.createElement("a");
      link.href = url; link.download = `${document.title.replace(/[^a-z0-9 _.-]/gi, "_") || "pysual"}.${format}`;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      document.getElementById("host-menu").open = false;
    } catch (error) { if (active) announce(error.message); }
  }, {signal: connection.signal});

  async function runOperation(command) {
    const controller = new AbortController(); operations.set(command.id, () => controller.abort());
    let answer;
    try { answer = {id: command.id, value: await client.service(command, controller.signal)}; }
    catch (error) { answer = {id: command.id, error: String(error.message || error)}; }
    operations.delete(command.id); complete.add(command.id);
    if (complete.size > 1024) complete.delete(complete.values().next().value);
    if (!controller.signal.aborted) { replies.push(answer); send(); }
  }
  function stop(message) {
    active = false;
    clearTimeout(disconnectTimer); disconnectTimer = null;
    connection.abort();
    events = []; replies = [];
    client.close();
    for (const cancel of operations.values()) cancel(); operations.clear();
    editor.blur(); document.getElementById("host-menu").hidden = true;
    announce(message);
  }
  if (!token) { stop("Open the full web URL from the running Python application."); return; }
  document.fonts.ready.then(() => { client.viewport(); push("repaint"); });
  startFrames();
})();
