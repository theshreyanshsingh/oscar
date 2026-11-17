"use strict";

const $ = (id) => document.getElementById(id);

const OPEN_STATUSES = new Set(["connecting", "ringing", "live"]);
const PHONE_PATTERN = /^\+[1-9]\d{7,14}$/;
const KIND_LABELS = { browser: "Browser", phone_inbound: "Incoming", phone_outbound: "Outgoing" };
const STATUS_LABELS = {
  connecting: "Connecting",
  ringing: "Ringing",
  live: "Live",
  ended: "Ended",
  failed: "Failed",
};
const FIELD_LABELS = {
  name: "Name",
  greeting: "Opening line",
  system_prompt: "Call instructions",
  knowledge: "What Oscar knows",
  script: "Script",
  model: "Chat model",
  voice: "Voice",
  accent: "Accent",
  to: "Number to call",
  instructions: "Chat instructions",
};
const SERVICE_NAMES = { OPENROUTER_API_KEY: "OpenRouter" };
const IDLE_CALL_TEXT = "Press Start call and allow the microphone.";
// Remembers, on this device, whether the caller lets themselves interrupt Oscar.
const INTERRUPT_CHOICE = "oscar.interrupt";

const state = {
  status: null,
  agentName: "Agent",
  calls: new Map(),
  // The call shown in "Live transcript" (one you started, or an incoming one).
  focusCallId: null,
  focusMessageIds: new Set(),
  liveSince: null,
  // The call opened from the history table.
  detailCallId: null,
  detailMessageIds: new Set(),
  // What that call's web searches found: [{ id, query, sources: [{ title, url }] }].
  detailSearches: [],
  // The browser call running in this tab.
  browserCallId: null,
  callUi: "idle",
};

let peer = null;
let micStream = null;
// Gives up on a call whose audio has not connected by then. It takes well under a
// second when it works; browsers can take half a minute to admit it never will.
const AUDIO_CONNECT_TIMEOUT_MS = 15000;
let audioConnectTimer = null;

// ---------- helpers ----------

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    throw new Error(describeError(body, response.status));
  }
  return body;
}

function describeError(body, status) {
  const detail = body && body.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : "";
        const label = FIELD_LABELS[field] || field;
        const text = friendlyValidationMessage(item.msg);
        const sentence = /[.!?]$/.test(text) ? text : `${text}.`;
        return label ? `${label}: ${sentence}` : sentence;
      })
      .join(" ");
  }
  return `Something went wrong (error ${status}). Please try again.`;
}

function friendlyValidationMessage(message) {
  if (message === "String should have at least 1 character") return "This field is required.";
  return message.replace(/^Value error, /, "");
}

function showError(id, message) {
  const element = $(id);
  element.textContent = message;
  element.hidden = !message;
}

function formatDuration(totalSeconds) {
  if (totalSeconds === null || totalSeconds === undefined) return "—";
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function formatTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function callNumber(call) {
  if (call.kind === "phone_inbound") return call.from_number || "Unknown";
  if (call.kind === "phone_outbound") return call.to_number;
  return "—";
}

function speakerLabel(call, role) {
  if (role === "agent") return `${state.agentName}:`;
  return call && call.kind === "browser" ? "You:" : "Caller:";
}

function renderLine(container, call, message) {
  const empty = container.querySelector(".empty");
  if (empty) empty.remove();
  const line = document.createElement("p");
  if (message.role === "note") {
    // Not speech: something Oscar did during the call, such as a web search.
    line.className = "line note";
    line.textContent = message.text;
  } else {
    line.className = message.role === "agent" ? "line from-agent" : "line from-caller";
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = speakerLabel(call, message.role);
    line.append(who, document.createTextNode(message.text));
  }
  container.append(line);
  container.scrollTop = container.scrollHeight;
}

function resetTranscript(container, emptyText) {
  container.replaceChildren();
  const empty = document.createElement("p");
  empty.className = "muted empty";
  empty.textContent = emptyText;
  container.append(empty);
}

// A link to a page a search found, or null when its address is not a web
// address. (The address comes from the search's results, not from us.)
function pageLink(page, label) {
  let url;
  try {
    url = new URL(page.url);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return null;
  const link = document.createElement("a");
  link.href = url.href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.dataset.site = url.hostname.replace(/^www\./, "");
  link.textContent = label || link.dataset.site;
  link.title = page.title || url.href;
  return link;
}

// What the call's searches found, each note with the pages it comes from
// (app/research.py).
function researchList(notes) {
  const list = document.createElement("ul");
  list.className = "research";
  for (const note of notes) {
    const item = document.createElement("li");
    item.append(note.text);
    for (const source of note.sources) {
      const link = pageLink(source);
      if (!link) continue;
      link.className = "source";
      item.append(" ", link);
    }
    list.append(item);
  }
  return list;
}

// Each thing Oscar searched for on the call, opening to the pages it found.
function searchList(searches) {
  const list = document.createElement("div");
  list.className = "searches";
  for (const search of searches) {
    const pages = search.sources.map((page) => [page, pageLink(page, page.title)]);
    const entry = document.createElement("details");
    const heading = document.createElement("summary");
    const count = pages.length === 1 ? "1 page" : `${pages.length} pages`;
    heading.textContent = `${search.query} · ${pages.length ? count : "nothing found"}`;
    const found = document.createElement("ul");
    for (const [page, link] of pages) {
      const item = document.createElement("li");
      item.append(link || page.title);
      if (link) {
        // Listings of one address all carry the same title: say whose page it is.
        const site = document.createElement("span");
        site.className = "muted";
        site.textContent = ` · ${link.dataset.site}`;
        item.append(site);
      }
      found.append(item);
    }
    entry.append(heading, found);
    list.append(entry);
  }
  return list;
}

// What a finished call was about, as [label, text or element] pairs. Oscar
// writes it when the call ends (app/summary.py).
function summaryRows(call) {
  if (call.summary_status === "writing") return [["Summary", "Writing the summary…"]];
  const rows = [];
  if (call.summary_status === "failed") {
    rows.push(["Summary", `The summary could not be written. ${call.summary_error}`.trim()]);
  } else if (call.summary) {
    rows.push(["Summary", call.summary]);
    if (call.caller_name) rows.push(["Name", call.caller_name]);
    if (call.callback_number) rows.push(["Call back on", call.callback_number]);
    if (call.follow_up) rows.push(["To do", call.follow_up]);
  }
  if (call.research && call.research.length) {
    rows.push(["Research", researchList(call.research)]);
  } else if (call.research_error) {
    rows.push(["Research", `The research notes could not be written. ${call.research_error}`]);
  }
  return rows;
}

// `searches` adds what the call searched for, with the pages found.
function renderSummary(container, call, searches = []) {
  const rows = call ? summaryRows(call) : [];
  if (call && searches.length) rows.push(["Searches", searchList(searches)]);
  const list = document.createElement("dl");
  list.className = "facts";
  for (const [label, content] of rows) {
    const term = document.createElement("dt");
    term.textContent = label;
    const value = document.createElement("dd");
    value.append(content);
    list.append(term, value);
  }
  container.replaceChildren(list);
  container.hidden = !rows.length;
}

// ---------- status & setup ----------

async function loadStatus() {
  const status = await api("/api/status");
  state.status = status;

  const agentPill = $("agent-pill");
  agentPill.textContent = status.agent.ready ? "Calls: ready" : "Calls: key missing";
  agentPill.classList.toggle("off", !status.agent.ready);

  const phonePill = $("phone-pill");
  phonePill.textContent = status.phone.enabled ? "Phone: ready" : "Phone: not set up";
  phonePill.classList.toggle("off", !status.phone.enabled);

  const searchPill = $("search-pill");
  searchPill.textContent = status.search.enabled ? "Web search: on" : "Web search: off";
  searchPill.classList.toggle("off", !status.search.enabled);
  $("whatsapp-search-hint").hidden = status.search.enabled;

  const notice = $("setup-notice");
  if (status.agent.ready) {
    notice.hidden = true;
  } else {
    const services = status.agent.missing.map((name) => SERVICE_NAMES[name] || name);
    const keys = services.length > 1 ? "keys" : "key";
    notice.textContent =
      `Voice calls are off until you add your ${services.join(" and ")} ${keys}. ` +
      "Run .venv/bin/python -m scripts.set_keys in the Oscar folder, then restart Oscar.";
    notice.hidden = false;
  }

  $("phone-ready").hidden = !status.phone.enabled;
  $("phone-setup").hidden = status.phone.enabled;
  if (status.phone.enabled) {
    $("phone-number").textContent = status.phone.number;
    $("phone-webhook").textContent = status.phone.voice_webhook;
  } else {
    const list = $("phone-missing");
    list.replaceChildren(
      ...status.phone.missing.map((name) => {
        const item = document.createElement("li");
        const code = document.createElement("code");
        code.textContent = name;
        item.append(code);
        return item;
      }),
    );
  }

  for (const [select, choices] of [
    [$("agent-voice"), status.voices],
    [$("agent-accent"), status.accents],
  ]) {
    if (!select.options.length) {
      for (const [id, label] of Object.entries(choices)) {
        select.append(new Option(label, id));
      }
    }
  }
  updateCallUi();
}

// ---------- agent settings ----------

async function loadAgent() {
  const agent = await api("/api/agent");
  fillAgentForm(agent);
}

function fillAgentForm(agent) {
  state.agentName = agent.name;
  $("agent-name").value = agent.name;
  $("agent-voice").value = agent.voice;
  $("agent-accent").value = agent.accent;
  $("agent-model").value = agent.model;
  $("agent-greeting").value = agent.greeting;
  $("agent-prompt").value = agent.system_prompt;
  $("agent-script").value = agent.script || "";
  $("agent-knowledge").value = agent.knowledge || "";
  countKnowledge();
}

const KNOWLEDGE_MAX_CHARACTERS = 12000;

function countKnowledge() {
  const used = $("agent-knowledge").value.length;
  $("agent-knowledge-count").textContent = used
    ? `${used.toLocaleString()} of ${KNOWLEDGE_MAX_CHARACTERS.toLocaleString()} characters used.`
    : "";
}

// Adds a text file's contents to the fact sheet. Nothing is uploaded: the
// browser reads the file, and it is saved with the rest of the form.
async function importKnowledge(event) {
  const [file] = event.target.files;
  event.target.value = ""; // so the same file can be chosen again
  if (!file) return;
  showError("agent-error", "");
  const box = $("agent-knowledge");
  const text = (await file.text()).replace(/\r\n?/g, "\n").trim();
  if (text.includes("\u0000")) {
    showError("agent-error", `${file.name} is not a text file. Copy its text and paste it instead.`);
    return;
  }
  const joined = [box.value.trim(), text].filter(Boolean).join("\n\n");
  if (joined.length > KNOWLEDGE_MAX_CHARACTERS) {
    showError(
      "agent-error",
      `${file.name} does not fit: Oscar takes ${KNOWLEDGE_MAX_CHARACTERS.toLocaleString()} characters, ` +
        `and this would make ${joined.length.toLocaleString()}. Keep the parts callers ask about.`,
    );
    return;
  }
  box.value = joined;
  countKnowledge();
  $("agent-saved").textContent = `Added ${file.name}. Save changes to keep it.`;
}

async function saveAgent(event) {
  event.preventDefault();
  showError("agent-error", "");
  $("agent-saved").textContent = "";
  const button = $("agent-save");
  button.disabled = true;
  try {
    const agent = await api("/api/agent", {
      method: "PUT",
      body: JSON.stringify({
        name: $("agent-name").value,
        voice: $("agent-voice").value,
        accent: $("agent-accent").value,
        model: $("agent-model").value,
        greeting: $("agent-greeting").value,
        system_prompt: $("agent-prompt").value,
        script: $("agent-script").value,
        knowledge: $("agent-knowledge").value,
      }),
    });
    fillAgentForm(agent);
    $("agent-saved").textContent = "Saved. The next call uses these settings.";
  } catch (error) {
    showError("agent-error", error.message);
  } finally {
    button.disabled = false;
  }
}

// ---------- call history ----------

async function loadCalls() {
  const { calls } = await api("/api/calls");
  state.calls = new Map(calls.map((call) => [call.id, call]));
  renderCalls();
}

function renderCalls() {
  const body = $("calls-body");
  const calls = [...state.calls.values()].sort((a, b) =>
    a.started_at < b.started_at ? 1 : a.started_at > b.started_at ? -1 : 0,
  );
  if (!calls.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.className = "muted";
    cell.textContent = "No calls yet. Your first one will show up here.";
    row.append(cell);
    body.replaceChildren(row);
    return;
  }
  body.replaceChildren(...calls.map(renderCallRow));
}

function renderCallRow(call) {
  const row = document.createElement("tr");
  if (call.id === state.detailCallId) row.className = "selected";

  const status = STATUS_LABELS[call.status] || call.status;
  const cells = [
    formatTime(call.started_at),
    KIND_LABELS[call.kind] || call.kind,
    callNumber(call),
    formatDuration(call.duration_seconds),
    call.error ? `${status}: ${call.error}` : status,
  ];
  for (const text of cells) {
    const cell = document.createElement("td");
    cell.textContent = text;
    row.append(cell);
  }
  row.cells[4].className = "wrap";
  row.insertBefore(summaryCell(call), row.cells[4]);

  const actions = document.createElement("td");
  const view = document.createElement("button");
  view.type = "button";
  view.className = "link";
  view.textContent = "Transcript";
  view.addEventListener("click", () => openDetail(call.id));
  actions.append(view);
  if (OPEN_STATUSES.has(call.status)) {
    const end = document.createElement("button");
    end.type = "button";
    end.className = "link";
    end.style.marginLeft = "12px";
    end.textContent = "End";
    end.addEventListener("click", () => endCall(call.id));
    actions.append(end);
  }
  row.append(actions);
  return row;
}

function summaryCell(call) {
  const cell = document.createElement("td");
  cell.className = "wrap summary";
  if (call.summary_status === "writing") {
    cell.textContent = "Writing…";
  } else if (call.summary_status === "failed") {
    cell.textContent = "Could not be written";
  } else if (call.summary) {
    cell.textContent = call.summary;
    if (call.follow_up) {
      const todo = document.createElement("span");
      todo.className = "todo";
      todo.textContent = `To do: ${call.follow_up}`;
      cell.append(todo);
    }
    if (call.research && call.research.length) {
      const research = document.createElement("span");
      research.className = "has-research";
      research.textContent =
        call.research.length === 1 ? "1 research note" : `${call.research.length} research notes`;
      cell.append(research);
    }
  } else {
    cell.textContent = "—";
  }
  return cell;
}

function renderDetailSummary(call) {
  renderSummary($("detail-summary"), call, state.detailSearches);
  const button = $("detail-summarise");
  // Nothing to sum up while the call is going, or when nothing was said on it.
  button.hidden =
    !call ||
    OPEN_STATUSES.has(call.status) ||
    call.summary_status === "writing" ||
    !state.detailMessageIds.size;
  if (call) {
    button.textContent =
      call.summary_status === "ready" ? "Write the summary again" : "Write a summary";
  }
}

async function writeSummary() {
  const callId = state.detailCallId;
  if (!callId) return;
  showError("detail-error", "");
  try {
    const { call } = await api(`/api/calls/${callId}/summary`, { method: "POST" });
    onCallChanged(call);
  } catch (error) {
    showError("detail-error", error.message);
  }
}

async function openDetail(callId) {
  state.detailCallId = callId;
  state.detailMessageIds = new Set();
  state.detailSearches = [];
  $("call-detail").hidden = false;
  showError("detail-error", "");
  renderDetailSummary(null);
  const container = $("detail-transcript");
  resetTranscript(container, "Loading…");
  renderCalls();
  try {
    const { call, messages, searches } = await api(`/api/calls/${callId}`);
    if (state.detailCallId !== callId) return;
    state.detailSearches = searches;
    $("detail-title").textContent =
      `${KIND_LABELS[call.kind]} call · ${formatTime(call.started_at)} · ` +
      `${STATUS_LABELS[call.status]}`;
    resetTranscript(container, "Nothing was said on this call.");
    for (const message of messages) {
      state.detailMessageIds.add(message.id);
      renderLine(container, call, message);
    }
    renderDetailSummary(call);
    if (call.error) showError("detail-error", call.error);
  } catch (error) {
    showError("detail-error", error.message);
  }
}

function closeDetail() {
  state.detailCallId = null;
  $("call-detail").hidden = true;
  renderCalls();
}

async function endCall(callId) {
  if (callId === state.browserCallId) {
    await endBrowserCall();
    return;
  }
  try {
    await api(`/api/calls/${callId}/end`, { method: "POST" });
  } catch (error) {
    showError("call-error", error.message);
  }
}

// ---------- live transcript ----------

async function focusCall(callId) {
  // Re-focusing the same call (after the live connection reconnects) must not
  // restart its timer.
  if (state.focusCallId !== callId) state.liveSince = null;
  state.focusCallId = callId;
  state.focusMessageIds = new Set();
  resetTranscript($("live-transcript"), "Waiting for the conversation to start…");
  renderSummary($("live-summary"), null);
  updateCallUi();
  try {
    const { call, messages } = await api(`/api/calls/${callId}`);
    if (state.focusCallId !== callId) return;
    state.calls.set(call.id, call);
    for (const message of messages) addFocusMessage(message);
    onCallChanged(call);
  } catch {
    // The event stream keeps the transcript current even if this first load fails.
  }
}

function addFocusMessage(message) {
  if (state.focusMessageIds.has(message.id)) return;
  state.focusMessageIds.add(message.id);
  renderLine($("live-transcript"), state.calls.get(message.call_id), message);
}

function focusedCallText(call) {
  const number = callNumber(call);
  switch (call.status) {
    case "connecting":
      if (call.kind === "phone_outbound") return `Dialing ${number}…`;
      if (call.kind === "phone_inbound") return `Incoming call from ${number}…`;
      return "Connecting…";
    case "ringing":
      return `Ringing ${number}…`;
    case "live":
      return call.kind === "browser" ? "You're live. Go ahead and talk." : `On the phone with ${number}.`;
    case "ended":
      return "Call ended.";
    case "failed":
      return "Call failed.";
    default:
      return call.status;
  }
}

function tickTimer() {
  const call = state.calls.get(state.focusCallId);
  if (call && call.status === "live" && state.liveSince) {
    const seconds = Math.floor((Date.now() - state.liveSince) / 1000);
    $("call-timer").textContent = formatDuration(seconds);
  } else if (!call || !OPEN_STATUSES.has(call.status)) {
    $("call-timer").textContent = "";
  }
}

// ---------- events from the server ----------

function onCallChanged(call) {
  state.calls.set(call.id, call);
  renderCalls();

  // Show an incoming phone call when nothing else is in progress here.
  const focused = state.calls.get(state.focusCallId);
  const focusIsOpen = focused && OPEN_STATUSES.has(focused.status);
  if (
    call.kind === "phone_inbound" &&
    OPEN_STATUSES.has(call.status) &&
    call.id !== state.focusCallId &&
    !focusIsOpen &&
    !state.browserCallId
  ) {
    focusCall(call.id);
    return;
  }

  if (call.id === state.focusCallId) {
    if (call.status === "live" && !state.liveSince) state.liveSince = Date.now();
    $("call-status").textContent = focusedCallText(call);
    renderSummary($("live-summary"), call);
    // Shown while the call is still live too, so a problem with listening or
    // speaking is visible straight away, not only after hanging up.
    if (call.error) showError("call-error", call.error);
  }

  if (call.id === state.browserCallId && !OPEN_STATUSES.has(call.status)) {
    // The server ended the call (time limit, error, or ended from another tab).
    cleanupBrowserCall();
  }
  if (call.id === state.detailCallId) {
    $("detail-title").textContent =
      `${KIND_LABELS[call.kind]} call · ${formatTime(call.started_at)} · ` +
      `${STATUS_LABELS[call.status]}`;
    renderDetailSummary(call);
    if (call.error) showError("detail-error", call.error);
  }
  updateCallUi();
  tickTimer();
}

function onMessage(message) {
  if (message.call_id === state.focusCallId) addFocusMessage(message);
  if (message.call_id === state.detailCallId && !state.detailMessageIds.has(message.id)) {
    state.detailMessageIds.add(message.id);
    const container = $("detail-transcript");
    renderLine(container, state.calls.get(message.call_id), message);
  }
}

// A search made on the call that is open from the history table.
function onSearch(search) {
  if (search.call_id !== state.detailCallId) return;
  if (state.detailSearches.some((known) => known.id === search.id)) return;
  state.detailSearches.push(search);
  renderDetailSummary(state.calls.get(search.call_id));
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onopen = () => {
    // Catch up on anything missed while disconnected.
    loadCalls().catch(() => {});
    if (state.focusCallId) focusCall(state.focusCallId);
  };
  source.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch {
      return;
    }
    if (data.type === "call") onCallChanged(data.call);
    else if (data.type === "message") onMessage(data.message);
    else if (data.type === "search") onSearch(data.search);
  };
}

// ---------- browser call (WebRTC) ----------

function updateCallUi() {
  const button = $("call-button");
  const label = button.querySelector(".btn-label");
  const ready = Boolean(state.status && state.status.agent.ready);
  if (state.callUi === "starting") {
    label.textContent = "Connecting…";
    button.disabled = true;
  } else if (state.callUi === "active") {
    label.textContent = "End call";
    button.disabled = false;
  } else {
    label.textContent = "Start call";
    button.disabled = !ready;
  }
  // The choice is made when a call starts, so it cannot change during one.
  $("call-interrupt").disabled = state.callUi !== "idle";
  const phoneButton = $("phone-button");
  if (phoneButton) phoneButton.disabled = !ready;
  updateOrb();
}

function restoreInterruptChoice() {
  const box = $("call-interrupt");
  try {
    box.checked = localStorage.getItem(INTERRUPT_CHOICE) === "yes";
  } catch {
    // Private browsing: the choice just isn't remembered.
  }
  box.addEventListener("change", () => {
    try {
      localStorage.setItem(INTERRUPT_CHOICE, box.checked ? "yes" : "no");
    } catch {
      // As above.
    }
  });
}

// ---------- orb ----------

let orbMeter = null;

function updateOrb() {
  const call = state.calls.get(state.focusCallId);
  let orbState = "idle";
  if (state.callUi === "starting") orbState = "connecting";
  else if (call && call.status === "live") orbState = "live";
  else if (call && OPEN_STATUSES.has(call.status)) orbState = "connecting";
  $("orb").dataset.state = orbState;
}

// Makes the orb swell with the loudness of the microphone and the agent's voice.
function startOrbMeter(stream) {
  try {
    if (!orbMeter) {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) return;
      const meter = { context: new AudioContextClass(), analysers: [], frame: 0, level: 0 };
      meter.context.resume().catch(() => {});
      const samples = new Uint8Array(512);
      const tick = () => {
        let loudest = 0;
        for (const analyser of meter.analysers) {
          analyser.getByteTimeDomainData(samples);
          let sum = 0;
          for (const value of samples) sum += (value - 128) ** 2;
          loudest = Math.max(loudest, Math.sqrt(sum / samples.length) / 128);
        }
        meter.level += (Math.min(1, loudest * 4) - meter.level) * 0.25;
        $("orb").style.setProperty("--level", meter.level.toFixed(3));
        meter.frame = requestAnimationFrame(tick);
      };
      meter.frame = requestAnimationFrame(tick);
      orbMeter = meter;
    }
    const analyser = orbMeter.context.createAnalyser();
    analyser.fftSize = 512;
    orbMeter.context.createMediaStreamSource(stream).connect(analyser);
    orbMeter.analysers.push(analyser);
  } catch {
    // The orb is decoration; the call works without it.
  }
}

function stopOrbMeter() {
  if (!orbMeter) return;
  cancelAnimationFrame(orbMeter.frame);
  orbMeter.context.close().catch(() => {});
  orbMeter = null;
  $("orb").style.setProperty("--level", "0");
}

function waitForIceGathering(connection, timeoutMs) {
  return new Promise((resolve) => {
    if (connection.iceGatheringState === "complete") {
      resolve();
      return;
    }
    const finish = () => {
      clearTimeout(timer);
      connection.removeEventListener("icegatheringstatechange", check);
      resolve();
    };
    const check = () => {
      if (connection.iceGatheringState === "complete") finish();
    };
    const timer = setTimeout(finish, timeoutMs);
    connection.addEventListener("icegatheringstatechange", check);
  });
}

function microphoneErrorMessage(error) {
  if (error && error.name === "NotAllowedError") {
    return "Your browser blocked the microphone. Allow it in the site settings, then try again.";
  }
  if (error && error.name === "NotFoundError") {
    return "No microphone found. Plug one in and try again.";
  }
  return (error && error.message) || "The call couldn't start. Please try again.";
}

async function startBrowserCall() {
  showError("call-error", "");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.RTCPeerConnection) {
    showError(
      "call-error",
      "This page can't use your microphone. Open Oscar at http://localhost (or over https) instead.",
    );
    return;
  }
  state.callUi = "starting";
  updateCallUi();
  $("call-status").textContent = "Waiting for microphone permission…";
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    startOrbMeter(micStream);
    peer = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });
    // The server expects an audio transceiver first, then a video one.
    peer.addTransceiver(micStream.getAudioTracks()[0], {
      direction: "sendrecv",
      streams: [micStream],
    });
    peer.addTransceiver("video", { direction: "sendrecv" });
    peer.ontrack = (event) => {
      if (event.track.kind !== "audio") return;
      const audio = $("agent-audio");
      audio.srcObject = event.streams[0] || new MediaStream([event.track]);
      audio.play().catch(() => {});
      startOrbMeter(audio.srcObject);
    };
    let audioConnected = false;
    peer.onconnectionstatechange = () => {
      if (!peer) return;
      if (peer.connectionState === "connected") {
        audioConnected = true;
        clearTimeout(audioConnectTimer);
      } else if (peer.connectionState === "failed") {
        if (audioConnected) {
          showError("call-error", "The call dropped. Check your connection and try again.");
          endBrowserCall();
        } else {
          audioNeverConnected();
        }
      }
    };

    $("call-status").textContent = "Connecting…";
    await peer.setLocalDescription(await peer.createOffer());
    await waitForIceGathering(peer, 3000);
    const answer = await api("/api/calls/browser", {
      method: "POST",
      body: JSON.stringify({
        sdp: peer.localDescription.sdp,
        type: "offer",
        interruptible: $("call-interrupt").checked,
      }),
    });
    state.browserCallId = answer.call_id;
    state.callUi = "active";
    focusCall(answer.call_id);
    await peer.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
    if (!audioConnected) audioConnectTimer = setTimeout(audioNeverConnected, AUDIO_CONNECT_TIMEOUT_MS);
  } catch (error) {
    const callId = state.browserCallId;
    cleanupBrowserCall();
    if (callId) api(`/api/calls/${callId}/end`, { method: "POST" }).catch(() => {});
    $("call-status").textContent = IDLE_CALL_TEXT;
    showError("call-error", microphoneErrorMessage(error));
  }
}

// The call's audio could not get between this browser and Oscar. The server
// records why (on a server, the firewall's UDP ports), and that reason replaces
// this message as soon as it arrives.
function audioNeverConnected() {
  if (!peer || !state.browserCallId) return; // that call is over already
  showError("call-error", "The call's audio could not connect to Oscar.");
  endBrowserCall("audio_failed");
}

function cleanupBrowserCall() {
  clearTimeout(audioConnectTimer);
  audioConnectTimer = null;
  if (peer) {
    peer.ontrack = null;
    peer.onconnectionstatechange = null;
    peer.close();
    peer = null;
  }
  if (micStream) {
    for (const track of micStream.getTracks()) track.stop();
    micStream = null;
  }
  stopOrbMeter();
  $("agent-audio").srcObject = null;
  state.browserCallId = null;
  state.callUi = "idle";
  updateCallUi();
}

async function endBrowserCall(reason) {
  const callId = state.browserCallId;
  cleanupBrowserCall();
  if (!callId) return;
  try {
    await api(`/api/calls/${callId}/end`, {
      method: "POST",
      body: reason ? JSON.stringify({ reason }) : undefined,
    });
  } catch {
    // Closing the connection also ends the call on the server.
  }
}

function onCallButton() {
  if (state.callUi === "active") endBrowserCall();
  else if (state.callUi === "idle") startBrowserCall();
}

// ---------- phone call ----------

async function startPhoneCall(event) {
  event.preventDefault();
  showError("phone-error", "");
  const to = $("phone-to").value.replace(/[\s()-]/g, "");
  if (!PHONE_PATTERN.test(to)) {
    showError("phone-error", "Start with + and the country code, like +1 415 555 0123.");
    return;
  }
  const button = $("phone-button");
  button.disabled = true;
  try {
    const { call } = await api("/api/calls/phone", {
      method: "POST",
      body: JSON.stringify({ to }),
    });
    state.calls.set(call.id, call);
    renderCalls();
    showError("call-error", "");
    focusCall(call.id);
  } catch (error) {
    showError("phone-error", error.message);
  } finally {
    updateCallUi();
  }
}

// ---------- WhatsApp ----------

const WHATSAPP_POLL_MS = 3000;
const WHATSAPP_LABELS = {
  stopped: "Offline",
  starting: "Connecting",
  waiting_for_scan: "Ready to link",
  connected: "Connected",
  logged_out: "Logged out",
  error: "Needs attention",
};
let whatsappFormFilled = false;
let whatsappQrVersion = "";

async function loadWhatsApp() {
  const data = await api("/api/whatsapp");
  renderWhatsApp(data);
  if (!whatsappFormFilled) {
    fillWhatsAppForm(data.settings);
    whatsappFormFilled = true;
  }
}

function fillWhatsAppForm(settings) {
  $("whatsapp-instructions").value = settings.instructions;
  $("whatsapp-web-search").checked = settings.web_search;
  $("whatsapp-private").checked = settings.reply_in_private_chats;
  $("whatsapp-own-number").checked = settings.own_number;
}

function renderWhatsApp(data) {
  const { state } = data;
  const pill = $("whatsapp-pill");
  // After a logout or error the process exits; keep saying why, not just "Offline".
  const shown =
    !state.running && ["logged_out", "error"].includes(state.last_status)
      ? state.last_status
      : state.status;
  pill.textContent = WHATSAPP_LABELS[shown] || shown;
  pill.classList.toggle("off", state.status !== "connected");

  let detail = state.detail;
  if (!state.running) {
    detail = ["logged_out", "error"].includes(state.last_status)
      ? state.detail
      : "Oscar isn't connected to WhatsApp right now.";
  } else if (state.status === "waiting_for_scan") {
    detail = "Scan the code below with Oscar's phone. The code renews itself, so there is no rush.";
  }
  if (state.status === "connected" && !data.openrouter_ready) {
    detail += " Add your OpenRouter key so Oscar can answer. Until then it can only apologise.";
  }
  $("whatsapp-detail").textContent = detail;

  $("whatsapp-not-running").hidden = state.running;
  $("whatsapp-install-hint").hidden = data.installed;
  $("whatsapp-scan").hidden = !state.has_qr;
  // Oscar either has a number to itself or shares its owner's: the advice differs.
  const connected = state.status === "connected";
  $("whatsapp-connected").hidden = !connected || data.settings.own_number;
  $("whatsapp-connected-own").hidden = !connected || !data.settings.own_number;
  // Running, but with neither a code nor a connection to show (connecting, or a problem).
  const busy = state.running && !state.has_qr && state.status !== "connected";
  $("whatsapp-busy").hidden = !busy;
  $("whatsapp-busy-text").textContent =
    state.status === "error" ? "Oscar's WhatsApp connection needs attention." : "Connecting to WhatsApp…";

  if (state.has_qr && state.qr_version !== whatsappQrVersion) {
    whatsappQrVersion = state.qr_version;
    $("whatsapp-qr").src = `/api/whatsapp/qr.svg?v=${encodeURIComponent(state.qr_version)}`;
  }
  if (!state.has_qr) whatsappQrVersion = "";

  $("whatsapp-phone").textContent = state.phone || "Oscar's number";
  $("whatsapp-phone-check").textContent = state.phone || "the linked number";
  $("whatsapp-phone-own").textContent = state.phone || "the linked number";
  const handle = (data.agent_name || "Oscar").split(/\s+/)[0];
  $("whatsapp-handle").textContent = `@${handle}`;
  $("whatsapp-handle-own").textContent = `@${handle}`;

  renderWhatsAppActivity(data.activity);
}

function renderWhatsAppActivity(activity) {
  const body = $("whatsapp-activity");
  if (!activity.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "muted";
    cell.textContent = "No questions yet. Mention @Oscar in a group to try it.";
    row.append(cell);
    body.replaceChildren(row);
    return;
  }
  body.replaceChildren(
    ...activity.map((item) => {
      const row = document.createElement("tr");
      const reply = item.error
        ? `${item.reply || "No reply sent."} Problem: ${item.error}`
        : item.reply;
      for (const [text, wrap] of [
        [formatTime(item.created_at), false],
        [item.chat_name, false],
        [item.asker, false],
        [item.request, true],
        [reply, true],
      ]) {
        const cell = document.createElement("td");
        cell.textContent = text;
        if (wrap) cell.className = "wrap";
        row.append(cell);
      }
      return row;
    }),
  );
}

async function saveWhatsApp(event) {
  event.preventDefault();
  showError("whatsapp-error", "");
  $("whatsapp-saved").textContent = "";
  const button = $("whatsapp-save");
  button.disabled = true;
  try {
    const settings = await api("/api/whatsapp/settings", {
      method: "PUT",
      body: JSON.stringify({
        instructions: $("whatsapp-instructions").value,
        web_search: $("whatsapp-web-search").checked,
        reply_in_private_chats: $("whatsapp-private").checked,
        own_number: $("whatsapp-own-number").checked,
      }),
    });
    fillWhatsAppForm(settings);
    $("whatsapp-saved").textContent = "Saved. Oscar uses this from the next message.";
  } catch (error) {
    showError("whatsapp-error", error.message);
  } finally {
    button.disabled = false;
  }
}

function pollWhatsApp() {
  setInterval(() => {
    if (document.visibilityState === "visible") loadWhatsApp().catch(() => {});
  }, WHATSAPP_POLL_MS);
}

// ---------- scroll reveal ----------

function revealOnScroll() {
  const items = document.querySelectorAll(".reveal");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!("IntersectionObserver" in window) || reduceMotion) return;
  document.documentElement.classList.add("can-reveal");
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        entry.target.classList.add("is-in");
        observer.unobserve(entry.target);
      }
    },
    { rootMargin: "0px 0px -8% 0px" },
  );
  for (const item of items) observer.observe(item);
}

// ---------- start ----------

async function init() {
  revealOnScroll();
  $("call-button").addEventListener("click", onCallButton);
  restoreInterruptChoice();
  $("agent-form").addEventListener("submit", saveAgent);
  $("agent-knowledge").addEventListener("input", countKnowledge);
  $("agent-knowledge-import").addEventListener("click", () => $("agent-knowledge-file").click());
  $("agent-knowledge-file").addEventListener("change", importKnowledge);
  $("phone-form").addEventListener("submit", startPhoneCall);
  $("detail-close").addEventListener("click", closeDetail);
  $("detail-summarise").addEventListener("click", writeSummary);
  $("whatsapp-form").addEventListener("submit", saveWhatsApp);
  window.addEventListener("beforeunload", () => {
    if (state.browserCallId) {
      navigator.sendBeacon(`/api/calls/${state.browserCallId}/end`);
    }
  });
  setInterval(tickTimer, 1000);

  try {
    // Status first: it fills the voice and accent lists, which the agent form then selects from.
    await loadStatus();
    await Promise.all([loadAgent(), loadCalls(), loadWhatsApp()]);
  } catch (error) {
    const notice = $("setup-notice");
    notice.textContent = `Could not reach the server: ${error.message}`;
    notice.hidden = false;
    updateCallUi(); // keeps the call buttons off until the server answers
  }
  connectEvents();
  pollWhatsApp();
}

init();
