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
  summary_fields: "Note from each call",
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
  // Call history as shown: the ids of the calls found, newest first, and how they were asked for.
  history: { ids: [], more: false, query: "", show: "all", openTodos: 0 },
  // The agent settings as last saved, to tell when the form has changed.
  agentSaved: null,
  whatsappSaved: null,
};

const HISTORY_PAGE = 50;

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

// Dollars, as precisely as a call's cost needs: "$0.031", "$1.24".
function formatCost(dollars) {
  if (typeof dollars !== "number" || !(dollars > 0)) return "—";
  if (dollars < 0.001) return "<$0.001";
  return dollars < 1 ? `$${dollars.toFixed(3)}` : `$${dollars.toFixed(2)}`;
}

function formatDollars(dollars) {
  return `$${Math.max(dollars, 0).toFixed(2)}`;
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
    const at = lineTime(call, message);
    if (at) who.append(" ", at);
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
    entry.dataset.searchId = String(search.id);
    const heading = document.createElement("summary");
    heading.dataset.key = `search-${search.id}`;
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
    if (call.follow_up) rows.push(["To do", todoControl(call)]);
    // What else the owner asked to have noted, by their own names for it.
    for (const [name, value] of Object.entries(call.details || {})) rows.push([name, value]);
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
  if (call && call.cost > 0) {
    const sofar = OPEN_STATUSES.has(call.status) || call.summary_status === "writing";
    rows.push(["Cost", `${formatCost(call.cost)}${sofar ? " so far" : ""}, on OpenRouter`]);
  }
  const list = document.createElement("dl");
  list.className = "facts";
  for (const [label, content] of rows) {
    const term = document.createElement("dt");
    term.textContent = label;
    const value = document.createElement("dd");
    value.append(content);
    list.append(term, value);
  }
  // A search opened to its pages stays open when the call is drawn again (its
  // cost changes every turn of a live call).
  const opened = new Set([...container.querySelectorAll("details[open]")].map((entry) => entry.dataset.searchId));
  for (const entry of list.querySelectorAll("details")) entry.open = opened.has(entry.dataset.searchId);
  keepFocus(container, () => container.replaceChildren(list));
  container.hidden = !rows.length;
}

// ---------- spending ----------

const SPENDING_POLL_MS = 30000;
let spendingTimer = null;

async function loadSpending() {
  const spending = await api("/api/spending");
  $("spend-day").textContent = formatDollars(spending.calls.last_24_hours);
  $("spend-month").textContent = formatDollars(spending.calls.last_30_days);
  renderSpendChart(spending.recent_calls || []);

  const key = spending.key;
  const left = $("spend-left");
  const note = $("spend-key-note");
  const pill = $("credit-pill");
  const low = Boolean(key && key.left !== null && key.left < spending.call_minimum);
  left.classList.toggle("low", low);
  if (!key) {
    left.textContent = "—";
    note.textContent = "OpenRouter did not say just now.";
    pill.hidden = true;
    return;
  }
  const spent = `${formatDollars(key.today)} spent today, ${formatDollars(key.this_month)} this month`;
  if (key.left === null) {
    left.textContent = "No limit";
    note.textContent = `${spent}. The key has no spending limit of its own.`;
    pill.hidden = true;
    return;
  }
  left.textContent = formatDollars(key.left);
  const resets = key.resets ? `, resets ${key.resets}` : "";
  note.textContent = low
    ? `Calls are off until the limit resets or is raised. Limit ${formatDollars(key.limit)}${resets}.`
    : `of its ${formatDollars(key.limit)} limit${resets}. ${spent}, counting everything that uses the key.`;
  pill.textContent = `Key: ${formatDollars(key.left)} left`;
  pill.classList.toggle("off", low);
  pill.hidden = false;
}

// A call's cost grows after it ends too (its summary), so look again shortly after a change.
function refreshSpendingSoon() {
  clearTimeout(spendingTimer);
  spendingTimer = setTimeout(() => loadSpending().catch(() => {}), 2000);
}

function refreshDashboard() {
  loadStatus().catch(() => {});
  loadSpending().catch(() => {});
}

function pollDashboard() {
  setInterval(() => {
    if (document.visibilityState === "visible") refreshDashboard();
  }, SPENDING_POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshDashboard();
  });
}

// ---------- summaries sent to the owner ----------

function renderNotify(webhook, { fill = false } = {}) {
  if (fill) $("notify-url").value = webhook.url;
  $("notify-test").hidden = !webhook.url;
  const status = $("notify-status");
  if (!webhook.url) {
    status.textContent = "Nothing is sent until you save an address.";
  } else if (!webhook.last_at) {
    status.textContent = "Saved. Summaries are sent from the next call.";
  } else if (webhook.last_result === "sent") {
    status.textContent = `Last sent ${formatTime(webhook.last_at)}: it arrived.`;
  } else {
    status.textContent = `Last tried ${formatTime(webhook.last_at)}: ${webhook.last_result}`;
  }
}

async function loadNotify() {
  renderNotify(await api("/api/summary-webhook"), { fill: true });
}

async function saveNotify(event) {
  event.preventDefault();
  showError("notify-error", "");
  const button = $("notify-save");
  button.disabled = true;
  try {
    const webhook = await api("/api/summary-webhook", {
      method: "PUT",
      body: JSON.stringify({ url: $("notify-url").value }),
    });
    renderNotify(webhook, { fill: true });
  } catch (error) {
    showError("notify-error", error.message);
  } finally {
    button.disabled = false;
  }
}

async function testNotify() {
  showError("notify-error", "");
  const button = $("notify-test");
  button.disabled = true;
  $("notify-status").textContent = "Sending a test…";
  try {
    const result = await api("/api/summary-webhook/test", { method: "POST" });
    renderNotify(result);
    if (!result.sent) showError("notify-error", result.problem);
  } catch (error) {
    showError("notify-error", error.message);
  } finally {
    button.disabled = false;
  }
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

  const voices = $("agent-voice");
  if (!voices.options.length) {
    for (const [id, label] of Object.entries(status.voices)) voices.append(new Option(label, id));
  }
  if (!$("agent-accent").options.length) fillAccents($("agent-accent"), status.accents);
  updateCallUi();
}

// ---------- agent settings ----------

async function loadAgent() {
  const agent = await api("/api/agent");
  fillAgentForm(agent);
}

const AGENT_FIELDS = [
  "agent-name",
  "agent-voice",
  "agent-accent",
  "agent-model",
  "agent-greeting",
  "agent-prompt",
  "agent-script",
  "agent-knowledge",
  "agent-summary-fields",
];

// What was measured for each accent (README.md, Agent settings; ACCENT_NOTE in
// app/agent.py). `tried` is, for each voice tried, [replies that held it, replies tried].
const ACCENT_FIT = {
  indian: {
    tried: {
      cedar: [80, 80], verse: [4, 4], marin: [4, 4], coral: [4, 4], sage: [4, 4],
      alloy: [3, 4], ash: [3, 4], nova: [3, 4], shimmer: [2, 4],
      ballad: [1, 4], echo: [1, 4], fable: [1, 4], onyx: [1, 4],
    },
  },
  australian: { note: "Every voice takes it, except Ballad, which stays British." },
  british: { note: "How well it holds depends on the voice: try one with Hear this voice." },
  scottish: {
    note: "Across the voices tried, 3 of 7 replies held it.",
    tried: { marin: [2, 2], coral: [1, 1], cedar: [0, 2], verse: [0, 1], ash: [0, 1] },
  },
  american: { note: "How well it holds depends on the voice: try one with Hear this voice." },
  southern_american: {
    note: "Across the voices tried, 6 of 7 replies held it. Replies can lean on Southern turns of phrase: one opened with “Howdy”.",
    tried: { cedar: [2, 2], marin: [2, 2], ash: [1, 1], verse: [1, 1], coral: [0, 1] },
  },
  singaporean: {
    note: "Across the voices tried, 4 of 7 replies held it.",
    tried: { cedar: [2, 2], ash: [1, 1], marin: [1, 2], coral: [0, 1], verse: [0, 1] },
  },
  nigerian: {
    note: "Across the voices tried, 4 of 7 replies held it.",
    tried: { cedar: [2, 2], ash: [1, 1], coral: [1, 1], marin: [0, 2], verse: [0, 1] },
  },
  french: {
    note: "Across the voices tried, 4 of 7 replies held it.",
    tried: { cedar: [2, 2], marin: [2, 2], verse: [0, 1], coral: [0, 1], ash: [0, 1] },
  },
  german: {
    note: "Across the voices tried, 4 of 7 replies held it.",
    tried: { marin: [2, 2], verse: [1, 1], cedar: [1, 2], ash: [0, 1], coral: [0, 1] },
  },
  none: { note: "Each voice as it comes. Nearly all of them sound American." },
};

// How the accent list is laid out. An accent the server offers that is not
// named here is listed after these, so none is ever hidden.
const ACCENT_GROUPS = [
  ["Accents of English", ["indian", "australian", "british", "scottish", "american", "southern_american", "singaporean", "nigerian"]],
  ["English with another first language", ["french", "german"]],
  ["No accent asked for", ["none"]],
];

function fillAccents(select, accents) {
  const listed = new Set();
  for (const [label, ids] of ACCENT_GROUPS) {
    const group = document.createElement("optgroup");
    group.label = label;
    for (const id of ids.filter((id) => id in accents)) {
      group.append(new Option(accents[id], id));
      listed.add(id);
    }
    if (group.children.length) select.append(group);
  }
  for (const [id, label] of Object.entries(accents)) {
    if (!listed.has(id)) select.append(new Option(label, id));
  }
}

function voiceName(voice) {
  const option = [...$("agent-voice").options].find((item) => item.value === voice);
  return option ? option.textContent.replace(/\s*\(.*\)$/, "") : voice;
}

function howItHeld(name, [held, tried]) {
  const replies = tried === 1 ? "the one reply" : tried === 2 ? "both replies" : `all ${tried} replies`;
  if (held === tried) return `${name} held it on ${replies} tried.`;
  if (held === 0) return `${name} lost it on ${replies} tried.`;
  return `${name} held it on ${held} of ${tried} replies tried.`;
}

// The hint under Accent: how the voice in the form did with it, the voices
// that held it every time (one click picks one), and what else was found.
function showAccentHint() {
  const hint = $("agent-accent-hint");
  const fit = ACCENT_FIT[$("agent-accent").value] || {};
  const voice = $("agent-voice").value;
  const mine = fit.tried && fit.tried[voice];
  hint.replaceChildren();
  if (fit.tried) {
    hint.append(mine ? howItHeld(voiceName(voice), mine) : `${voiceName(voice)} has not been tried with this accent.`);
  }
  const best = !fit.tried || (mine && mine[0] === mine[1])
    ? []
    : Object.entries(fit.tried)
        .filter(([id, [held, tried]]) => id !== voice && held === tried)
        .sort((a, b) => b[1][1] - a[1][1])
        .slice(0, 4);
  if (best.length) {
    const offer = document.createElement("span");
    offer.className = "accent-best";
    offer.append("Held every time by ");
    best.forEach(([id], index) => {
      if (index) offer.append(index === best.length - 1 ? " and " : ", ");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "link";
      button.dataset.voice = id;
      const label = $("agent-voice").querySelector(`option[value="${id}"]`)?.textContent || voiceName(id);
      button.textContent = label;
      button.setAttribute("aria-label", `Use ${label}`);
      offer.append(button);
    });
    offer.append(".");
    hint.append(" ", offer);
  }
  if (fit.note) hint.append(hint.childNodes.length ? " " : "", fit.note);
}

// "Held every time by Marin": picks that voice, as if chosen from the list.
function useVoice(event) {
  const button = event.target.closest("button[data-voice]");
  if (!button) return;
  const select = $("agent-voice");
  select.value = button.dataset.voice;
  select.dispatchEvent(new Event("change", { bubbles: true }));
  select.focus();
}

function formSnapshot(ids) {
  return JSON.stringify(ids.map((id) => {
    const field = $(id);
    return field.type === "checkbox" ? field.checked : field.value;
  }));
}

function agentDirty() {
  return state.agentSaved !== null && formSnapshot(AGENT_FIELDS) !== state.agentSaved;
}

function updateAgentDirty() {
  const dirty = agentDirty();
  $("agent-form").classList.toggle("dirty", dirty);
  $("agent-dirty").hidden = !dirty;
  $("agent-discard").hidden = !dirty;
  if (dirty) $("agent-saved").textContent = "";
}

// "1,234 / 8,000" under a long field, red when nearly full.
const COUNTED_FIELDS = [
  ["agent-greeting", 500],
  ["agent-prompt", 8000],
  ["agent-script", 4000],
  ["whatsapp-instructions", 4000],
];

function setUpCounters() {
  for (const [id, most] of COUNTED_FIELDS) {
    const field = $(id);
    const counter = document.createElement("p");
    counter.className = "counter";
    counter.id = `${id}-count`;
    field.after(counter);
    field.setAttribute("aria-describedby", counter.id);
    field.addEventListener("input", () => updateCounter(id, most));
  }
}

function updateCounter(id, most) {
  const used = $(id).value.length;
  const counter = $(`${id}-count`);
  counter.textContent = `${used.toLocaleString()} / ${most.toLocaleString()}`;
  counter.classList.toggle("near", used > most * 0.9);
}

function updateCounters() {
  for (const [id, most] of COUNTED_FIELDS) updateCounter(id, most);
}

function agentFormBody() {
  return {
    name: $("agent-name").value,
    voice: $("agent-voice").value,
    accent: $("agent-accent").value,
    model: $("agent-model").value,
    greeting: $("agent-greeting").value,
    system_prompt: $("agent-prompt").value,
    script: $("agent-script").value,
    knowledge: $("agent-knowledge").value,
    // One a line; the server tidies blanks and repeats away.
    summary_fields: $("agent-summary-fields").value.split("\n"),
  };
}

let previewAudio = null;

// Plays a short sample with the voice and accent in the form, before they are saved.
async function previewVoice() {
  const button = $("agent-preview");
  if (previewAudio && !previewAudio.paused) {
    previewAudio.pause();
    button.textContent = "Hear this voice";
    return;
  }
  showError("agent-error", "");
  button.disabled = true;
  button.textContent = "Getting a sample…";
  try {
    const response = await fetch("/api/agent/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(agentFormBody()),
    });
    if (!response.ok) {
      let body = null;
      try {
        body = await response.json();
      } catch {
        body = null;
      }
      throw new Error(describeError(body, response.status));
    }
    const url = URL.createObjectURL(await response.blob());
    previewAudio = new Audio(url);
    previewAudio.addEventListener("ended", () => {
      button.textContent = "Hear this voice";
      URL.revokeObjectURL(url);
    });
    await previewAudio.play();
    button.textContent = "Stop";
  } catch (error) {
    button.textContent = "Hear this voice";
    showError("agent-error", error.message);
  } finally {
    button.disabled = false;
  }
}

function fillAgentForm(agent) {
  state.agentSettings = agent;
  state.agentName = agent.name;
  $("agent-name").value = agent.name;
  $("agent-voice").value = agent.voice;
  $("agent-accent").value = agent.accent;
  $("agent-model").value = agent.model;
  $("agent-greeting").value = agent.greeting;
  $("agent-prompt").value = agent.system_prompt;
  $("agent-script").value = agent.script || "";
  $("agent-knowledge").value = agent.knowledge || "";
  $("agent-summary-fields").value = (agent.summary_fields || []).join("\n");
  countKnowledge();
  showAccentHint();
  updateCounters();
  state.agentSaved = formSnapshot(AGENT_FIELDS);
  updateAgentDirty();
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
  // Setting the value fires no input event, and the form's own change check ran
  // before the file was read: show the unsaved-changes bar here.
  updateAgentDirty();
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
      body: JSON.stringify(agentFormBody()),
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

// Loads the newest page of Call history for the search and filter in force, or with
// `older`, the page after the calls already shown.
async function loadCalls({ older = false } = {}) {
  const history = state.history;
  const asked = `${history.query}\n${history.show}`;
  const params = new URLSearchParams({ limit: String(HISTORY_PAGE), show: history.show });
  if (history.query) params.set("q", history.query);
  // The download holds every call the search and filter find, not only the pages shown.
  const exported = new URLSearchParams({ show: history.show });
  if (history.query) exported.set("q", history.query);
  $("history-export").href = `/api/calls.csv?${exported}`;
  const last = older ? state.calls.get(history.ids[history.ids.length - 1]) : null;
  if (last) {
    params.set("before_id", last.id);
    params.set("before", last.started_at);
  }
  const page = await api(`/api/calls?${params}`);
  if (`${history.query}\n${history.show}` !== asked) return; // searched again meanwhile
  // Calls are kept (a live call stays known even when a filter hides it).
  for (const call of page.calls) state.calls.set(call.id, call);
  const ids = page.calls.map((call) => call.id);
  history.ids = older ? [...history.ids, ...ids.filter((id) => !history.ids.includes(id))] : ids;
  history.more = page.more;
  history.openTodos = page.open_todos;
  renderCalls();
}

function hasOpenTodo(call) {
  return Boolean(call && call.follow_up && !call.follow_up_done_at);
}

// Whether a call that has just started or changed belongs in the list as searched and filtered.
function matchesHistory(call) {
  const { show, query } = state.history;
  const kinds = { incoming: "phone_inbound", outgoing: "phone_outbound", browser: "browser" };
  if (show === "todo" && !hasOpenTodo(call)) return false;
  if (kinds[show] && call.kind !== kinds[show]) return false;
  if (show === "failed" && call.status !== "failed") return false;
  if (!query) return true;
  const fields = [call.summary, call.follow_up, call.caller_name, call.error];
  const digits = query.replace(/[\s()\-.+]/g, "");
  const numbers = [call.from_number, call.to_number, call.callback_number].join(" ");
  return (
    fields.join(" ").toLowerCase().includes(query.toLowerCase()) ||
    (/^\d{3,}$/.test(digits) && numbers.includes(digits))
  );
}

function renderCalls() {
  const body = $("calls-body");
  const history = state.history;
  const calls = history.ids.map((id) => state.calls.get(id)).filter(Boolean);
  const filtered = Boolean(history.query) || history.show !== "all";
  if (!calls.length) {
    const row = document.createElement("tr");
    row.setAttribute("role", "row");
    const cell = document.createElement("td");
    cell.setAttribute("role", "cell");
    cell.colSpan = 8;
    cell.className = "muted";
    cell.textContent = filtered
      ? "No calls match. Try other words, or show all calls."
      : "No calls yet. Your first one will show up here.";
    row.append(cell);
    keepFocus(body, () => body.replaceChildren(row));
  } else {
    keepFocus(body, () => body.replaceChildren(...calls.map(renderCallRow)));
  }
  $("history-older").hidden = !history.more;
  $("history-export").hidden = !calls.length; // nothing to download
  $("history-count").textContent = calls.length
    ? `${calls.length} call${calls.length === 1 ? "" : "s"} shown${history.more ? ", older ones below" : ""}.`
    : "";
  const count = $("todo-count");
  count.textContent = String(history.openTodos);
  count.hidden = !history.openTodos;
}

let historySearchTimer = null;

function setUpHistory() {
  $("history-search").addEventListener("input", (event) => {
    clearTimeout(historySearchTimer);
    historySearchTimer = setTimeout(() => {
      state.history.query = event.target.value.trim();
      loadCalls().catch((error) => showError("call-error", error.message));
    }, 250);
  });
  for (const chip of document.querySelectorAll(".chip")) {
    chip.addEventListener("click", () => {
      for (const other of document.querySelectorAll(".chip")) {
        other.setAttribute("aria-pressed", String(other === chip));
      }
      state.history.show = chip.dataset.show;
      loadCalls().catch(() => {});
    });
  }
  $("history-older").addEventListener("click", async () => {
    const button = $("history-older");
    button.disabled = true;
    try {
      await loadCalls({ older: true });
    } finally {
      button.disabled = false;
    }
  });
}

// Ticks a call's to-do off, or back on.
async function tickTodo(callId, done) {
  try {
    const { call } = await api(`/api/calls/${callId}/todo`, {
      method: "PUT",
      body: JSON.stringify({ done }),
    });
    onCallChanged(call);
  } catch (error) {
    // Every box for this to-do (table, side panel, live call) goes back to what
    // the server last said, and the reason shows in the side panel when it is open.
    const known = state.calls.get(callId);
    if (known) onCallChanged(known);
    else renderCalls();
    showError(state.detailCallId ? "detail-error" : "call-error", error.message);
  }
}

// The to-do, with a box to tick it off.
function todoControl(call) {
  const label = document.createElement("label");
  label.className = `todo-check${call.follow_up_done_at ? " done" : ""}`;
  const box = document.createElement("input");
  box.type = "checkbox";
  box.dataset.key = `todo-${call.id}`;
  box.checked = Boolean(call.follow_up_done_at);
  box.setAttribute("aria-label", `Done: ${call.follow_up}`);
  box.addEventListener("change", () => tickTodo(call.id, box.checked));
  label.append(box, document.createTextNode(call.follow_up));
  return label;
}

const CALL_COLUMNS = ["Started", "Type", "Number", "Length", "Cost", "Status"];

// Which call a button is about, for screen readers: "Incoming call, Nov 21, 5:38 PM, +61412345678".
function callLabel(call) {
  const number = callNumber(call);
  const kind = KIND_LABELS[call.kind] || "A";
  return `${kind} call, ${formatTime(call.started_at)}${number && number !== "—" ? `, ${number}` : ""}`;
}

function renderCallRow(call) {
  const row = document.createElement("tr");
  row.setAttribute("role", "row");
  if (call.id === state.detailCallId) row.className = "selected";

  const status = STATUS_LABELS[call.status] || call.status;
  const cells = [
    formatTime(call.started_at),
    KIND_LABELS[call.kind] || call.kind,
    callNumber(call),
    formatDuration(call.duration_seconds),
    formatCost(call.cost),
    call.error ? `${status}: ${call.error}` : status,
  ];
  cells.forEach((text, index) => {
    const cell = document.createElement("td");
    cell.setAttribute("role", "cell");
    cell.dataset.label = CALL_COLUMNS[index];
    cell.textContent = text;
    row.append(cell);
  });
  row.cells[4].className = "cost";
  row.cells[5].className = "wrap";
  row.insertBefore(summaryCell(call), row.cells[5]);

  const actions = document.createElement("td");
  actions.setAttribute("role", "cell");
  actions.dataset.label = "";
  const view = document.createElement("button");
  view.type = "button";
  view.className = "link";
  view.textContent = "Transcript";
  view.setAttribute("aria-label", `Transcript: ${callLabel(call)}`);
  view.dataset.callId = call.id;
  view.dataset.key = `transcript-${call.id}`;
  view.addEventListener("click", () => openDetail(call.id));
  actions.append(view);
  if (OPEN_STATUSES.has(call.status)) {
    const end = document.createElement("button");
    end.type = "button";
    end.className = "link";
    end.style.marginLeft = "12px";
    end.textContent = "End";
    end.setAttribute("aria-label", `End: ${callLabel(call)}`);
    end.dataset.key = `end-${call.id}`;
    end.addEventListener("click", () => endCall(call.id));
    actions.append(end);
  }
  row.append(actions);
  return row;
}

function summaryCell(call) {
  const cell = document.createElement("td");
  cell.setAttribute("role", "cell");
  cell.dataset.label = "Summary";
  cell.className = "wrap summary";
  if (call.summary_status === "writing") {
    cell.textContent = "Writing…";
  } else if (call.summary_status === "failed") {
    cell.textContent = "Could not be written";
  } else if (call.summary) {
    cell.textContent = call.summary;
    if (call.follow_up) {
      const todo = todoControl(call);
      todo.firstChild.after(document.createTextNode("To do: "));
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
  // Off until the server answers: a second press would be refused as "already being written".
  const button = $("detail-summarise");
  button.disabled = true;
  try {
    const { call } = await api(`/api/calls/${callId}/summary`, { method: "POST" });
    onCallChanged(call);
  } catch (error) {
    showError("detail-error", error.message);
  } finally {
    button.disabled = false;
  }
}

// Whether Tab can reach it: shown, enabled, and not inside a closed <details>
// (of which only the summary line can be reached).
function canTabTo(element) {
  if (element.disabled || element.closest("[hidden]")) return false;
  const closed = element.closest("details:not([open])");
  return !closed || (element.tagName === "SUMMARY" && element.parentElement === closed);
}

// Keeps Tab inside the open side panel, and Escape closes it.
function onDrawerKey(event) {
  if (event.key === "Escape") {
    event.preventDefault();
    closeDetail();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [
    ...$("call-detail").querySelectorAll("button, a[href], input, summary, [tabindex='0']"),
  ].filter(canTabTo);
  if (!focusable.length) return;
  // -1 too when focus is on the panel's title (where it opens) or nowhere: wrap from there as well.
  const at = focusable.indexOf(document.activeElement);
  if (event.shiftKey && at <= 0) {
    event.preventDefault();
    focusable[focusable.length - 1].focus();
  } else if (!event.shiftKey && (at === -1 || at === focusable.length - 1)) {
    event.preventDefault();
    focusable[0].focus();
  }
}

// The page behind the open side panel: inert, so neither Tab nor a screen
// reader can wander out of the panel into it.
function setPageInert(inert) {
  for (const element of document.body.children) {
    if (element.id !== "call-detail" && element.id !== "detail-backdrop") element.inert = inert;
  }
}

// Draws a container again without dropping keyboard focus: the new copy of the
// control that had it (same data-key) gets it back. Calls change every turn.
function keepFocus(container, draw) {
  const key = container.contains(document.activeElement) ? document.activeElement.dataset.key : "";
  draw();
  const again = key && container.querySelector(`[data-key="${CSS.escape(key)}"]`);
  if (again) again.focus({ preventScroll: true });
}

// The number to call back: the one the caller left, or the line the call was on.
function callBackNumber(call) {
  if (!call) return "";
  if (call.callback_number) return call.callback_number;
  if (call.kind === "phone_inbound") return call.from_number;
  if (call.kind === "phone_outbound") return call.to_number;
  return "";
}

function renderDetailActions(call) {
  const number = callBackNumber(call);
  const phoneReady = Boolean(state.status && state.status.phone.enabled);
  const callback = $("detail-callback");
  callback.hidden = !number || !phoneReady;
  callback.textContent = `Call back ${number}`;
  $("detail-copy").hidden = !number || !navigator.clipboard;
  $("detail-link").hidden = !navigator.clipboard;
  // Not while the call is on, or while its summary is being written.
  const deletable = Boolean(call) && !OPEN_STATUSES.has(call.status) && call.summary_status !== "writing";
  $("detail-delete").hidden = !deletable;
  if (!deletable) $("detail-confirm").hidden = true;
  $("detail-actions").hidden = [...$("detail-actions").children].every((button) => button.hidden);
}

// Puts the number in "Call a number", for a look before Oscar dials it.
function callBack() {
  const number = callBackNumber(state.calls.get(state.detailCallId));
  closeDetail({ restoreFocus: false });
  $("phone-to").value = number;
  $("phone").scrollIntoView({ behavior: "smooth", block: "start" });
  $("phone-to").focus({ preventScroll: true });
  if (!PHONE_PATTERN.test(number.replace(/[\s()-]/g, ""))) {
    showError("phone-error", "Add the country code (it starts with +), then press Call now.");
  }
}

async function copyNumber() {
  const number = callBackNumber(state.calls.get(state.detailCallId));
  try {
    await navigator.clipboard.writeText(number);
    $("detail-copy").textContent = "Copied";
    setTimeout(() => ($("detail-copy").textContent = "Copy number"), 1500);
  } catch {
    $("detail-copy").textContent = "Couldn't copy";
  }
}

async function openDetail(callId) {
  state.detailCallId = callId;
  state.detailMessageIds = new Set();
  state.detailSearches = [];
  $("call-detail").hidden = false;
  $("detail-backdrop").hidden = false;
  setPageInert(true);
  document.documentElement.classList.add("drawer-open");
  document.addEventListener("keydown", onDrawerKey);
  showCallInAddress(callId);
  $("detail-confirm").hidden = true;
  $("detail-title").textContent = "Transcript";
  $("detail-title").focus();
  renderDetailActions(state.calls.get(callId));
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
    renderDetailActions(call);
    if (call.error) showError("detail-error", call.error);
  } catch (error) {
    showError("detail-error", error.message);
  }
}

function closeDetail({ restoreFocus = true } = {}) {
  const callId = state.detailCallId;
  state.detailCallId = null;
  $("call-detail").hidden = true;
  $("detail-backdrop").hidden = true;
  setPageInert(false); // before focus goes back to the page
  showCallInAddress(null);
  $("detail-confirm").hidden = true;
  document.documentElement.classList.remove("drawer-open");
  document.removeEventListener("keydown", onDrawerKey);
  renderCalls();
  // Back to the button that opened it (the table was drawn again meanwhile).
  if (restoreFocus && callId) {
    const opener = document.querySelector(`#calls-body button[data-call-id="${callId}"]`);
    if (opener) opener.focus();
  }
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

// When the call began, by the server's record, so a call already going when this
// page picks it up (after a reload, say) is not timed from zero. Now if unreadable.
function callStartedMs(call) {
  const started = Date.parse(call.started_at);
  return Number.isNaN(started) ? Date.now() : started;
}

function tickTimer() {
  const call = state.calls.get(state.focusCallId);
  if (call && call.status === "live" && state.liveSince) {
    // Never below zero when this computer's clock is behind the server's.
    const seconds = Math.max(0, Math.floor((Date.now() - state.liveSince) / 1000));
    $("call-timer").textContent = formatDuration(seconds);
  } else if (!call || !OPEN_STATUSES.has(call.status)) {
    $("call-timer").textContent = "";
  }
  updateTitle(call);
}

// "● Live 1:24 · Oscar" while a call is on, so it shows from another tab.
function updateTitle(call) {
  let title = "Oscar";
  if (call && call.status === "live") {
    title = `● Live ${$("call-timer").textContent} · Oscar`.replace("  ", " ");
  } else if (call && OPEN_STATUSES.has(call.status)) {
    title = "Connecting… · Oscar";
  }
  if (document.title !== title) document.title = title;
}

// ---------- events from the server ----------

function onCallChanged(call) {
  const before = state.calls.get(call.id);
  if (!before && call.kind === "phone_inbound" && OPEN_STATUSES.has(call.status)) ringFor(call);
  if (before && before.cost !== call.cost) refreshSpendingSoon();
  state.calls.set(call.id, call);
  const history = state.history;
  history.openTodos += Number(hasOpenTodo(call)) - Number(hasOpenTodo(before));
  if (history.openTodos < 0) history.openTodos = 0;
  if (!history.ids.includes(call.id) && matchesHistory(call)) {
    const newest = state.calls.get(history.ids[0]);
    if (!newest || call.started_at >= newest.started_at) history.ids.unshift(call.id);
  }
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
    if (call.status === "live" && !state.liveSince) state.liveSince = callStartedMs(call);
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
    // The summary can bring a callback number the call did not have.
    renderDetailActions(call);
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

let connectionTimer = null;

function showConnectionNotice(text, { reload = false } = {}) {
  $("connection-text").textContent = text;
  $("connection-reload").hidden = !reload;
  $("connection-notice").hidden = false;
}

function hideConnectionNotice() {
  clearTimeout(connectionTimer);
  $("connection-notice").hidden = true;
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onopen = () => {
    hideConnectionNotice();
    // Catch up on anything missed while disconnected.
    loadCalls().catch(() => {});
    refreshDashboard();
    if (state.focusCallId) focusCall(state.focusCallId);
  };
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) {
      // The browser gave up (for example, a sign-in in front of Oscar expired).
      showConnectionNotice("Lost the connection to Oscar.", { reload: true });
      return;
    }
    // It tries again by itself: only say so if that takes more than a moment.
    clearTimeout(connectionTimer);
    connectionTimer = setTimeout(() => {
      if (source.readyState !== EventSource.OPEN) {
        showConnectionNotice("Lost the connection to Oscar. Trying again…");
      }
    }, 4000);
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
    else if (data.type === "call_deleted") onCallDeleted(data.id);
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
  $("call-mute").hidden = state.callUi !== "active";
  const phoneButton = $("phone-button");
  if (phoneButton) phoneButton.disabled = !ready || phoneCallPending;
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
    micStream = await openMicrophone();
    listMicrophones(); // their names are known now
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
  setMuted(false);
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

// True while "Call now" waits for the server. The call shows up over the event
// stream before Twilio has even answered the server, and that redraws the
// buttons: without this, a second press would dial the number again.
let phoneCallPending = false;

async function startPhoneCall(event) {
  event.preventDefault();
  if (phoneCallPending) return;
  showError("phone-error", "");
  const to = $("phone-to").value.replace(/[\s()-]/g, "");
  if (!PHONE_PATTERN.test(to)) {
    showError("phone-error", "Start with + and the country code, like +1 415 555 0123.");
    return;
  }
  phoneCallPending = true;
  updateCallUi();
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
    phoneCallPending = false;
    updateCallUi();
  }
}

// ---------- WhatsApp ----------

// How often WhatsApp is asked about: often while a code is being scanned, rarely when it isn't set up.
function whatsappPollMs(data) {
  if (!data) return 10000;
  const { state, installed } = data;
  if (state.running && ["starting", "waiting_for_scan"].includes(state.status)) return 3000;
  if (state.running) return 15000;
  return installed ? 10000 : 60000;
}
let whatsappLatest = null;
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
  whatsappLatest = data;
  renderWhatsApp(data);
  if (!whatsappFormFilled) {
    fillWhatsAppForm(data.settings);
    whatsappFormFilled = true;
  }
}

const WHATSAPP_FIELDS = ["whatsapp-instructions", "whatsapp-web-search", "whatsapp-private", "whatsapp-own-number"];

function fillWhatsAppForm(settings) {
  $("whatsapp-instructions").value = settings.instructions;
  $("whatsapp-web-search").checked = settings.web_search;
  $("whatsapp-private").checked = settings.reply_in_private_chats;
  $("whatsapp-own-number").checked = settings.own_number;
  updateCounters();
  state.whatsappSaved = formSnapshot(WHATSAPP_FIELDS);
}

function unsavedChanges() {
  const whatsapp = state.whatsappSaved !== null && formSnapshot(WHATSAPP_FIELDS) !== state.whatsappSaved;
  return agentDirty() || whatsapp;
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
    row.setAttribute("role", "row");
    const cell = document.createElement("td");
    cell.setAttribute("role", "cell");
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
      row.setAttribute("role", "row");
      const reply = item.error
        ? `${item.reply || "No reply sent."} Problem: ${item.error}`
        : item.reply;
      for (const [label, text, wrap] of [
        ["When", formatTime(item.created_at), false],
        ["Chat", item.chat_name, false],
        ["From", item.asker, false],
        ["Question", item.request, true],
        ["Oscar's reply", reply, true],
      ]) {
        const cell = document.createElement("td");
        cell.setAttribute("role", "cell");
        cell.dataset.label = label;
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
  setTimeout(async () => {
    if (document.visibilityState === "visible") await loadWhatsApp().catch(() => {});
    pollWhatsApp();
  }, whatsappPollMs(whatsappLatest));
}

// ---------- menu (small screens) ----------

function setUpMenu() {
  const nav = document.querySelector(".nav");
  const toggle = $("nav-toggle");
  const close = () => {
    nav.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
  };
  toggle.addEventListener("click", () => {
    toggle.setAttribute("aria-expanded", String(nav.classList.toggle("open")));
  });
  for (const link of nav.querySelectorAll("a")) link.addEventListener("click", close);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && nav.classList.contains("open")) {
      close();
      toggle.focus();
    }
  });
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

// ---------- microphone: which one, and mute ----------

const MIC_CHOICE = "oscar.microphone";
let muted = false;

function chosenMicrophone() {
  try {
    return localStorage.getItem(MIC_CHOICE) || "";
  } catch {
    return "";
  }
}

// The microphone chosen in the list, or the browser's own pick when there is none
// (or the chosen one is unplugged).
async function openMicrophone(deviceId = chosenMicrophone()) {
  const audio = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  if (deviceId) {
    try {
      return await navigator.mediaDevices.getUserMedia({ audio: { ...audio, deviceId: { exact: deviceId } } });
    } catch (error) {
      if (error.name !== "OverconstrainedError" && error.name !== "NotFoundError") throw error;
    }
  }
  return navigator.mediaDevices.getUserMedia({ audio });
}

// The microphones to pick from. Browsers name them only once the page may use one,
// so the list shows after the first call, and only when there is a choice.
async function listMicrophones() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  let devices;
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch {
    return;
  }
  const mics = devices.filter((device) => device.kind === "audioinput" && device.deviceId && device.label);
  const select = $("call-mic");
  const track = micStream && micStream.getAudioTracks()[0];
  const using = (track && track.getSettings && track.getSettings().deviceId) || "";
  const wanted = mics.some((mic) => mic.deviceId === chosenMicrophone()) ? chosenMicrophone() : using;
  select.replaceChildren(...mics.map((mic) => new Option(mic.label, mic.deviceId)));
  if (mics.some((mic) => mic.deviceId === wanted)) select.value = wanted;
  $("mic-choice").hidden = mics.length < 2;
}

// A microphone picked from the list: kept for next time, and during a call, swapped in at once.
async function onMicrophoneChosen() {
  const deviceId = $("call-mic").value;
  try {
    localStorage.setItem(MIC_CHOICE, deviceId);
  } catch {
    // Private browsing: used for this call only.
  }
  const sender = peer && peer.getSenders().find((each) => each.track && each.track.kind === "audio");
  if (!sender || !micStream) return;
  try {
    const stream = await openMicrophone(deviceId);
    if (!peer || !micStream) {
      for (const track of stream.getTracks()) track.stop(); // the call ended meanwhile
      return;
    }
    const track = stream.getAudioTracks()[0];
    track.enabled = !muted;
    await sender.replaceTrack(track);
    for (const old of micStream.getTracks()) old.stop();
    micStream = stream;
    startOrbMeter(stream);
  } catch (error) {
    showError("call-error", microphoneErrorMessage(error));
  }
}

// Muted, Oscar hears silence: the call goes on, and nothing you say reaches it.
function setMuted(on) {
  muted = on;
  if (micStream) for (const track of micStream.getAudioTracks()) track.enabled = !on;
  const button = $("call-mute");
  button.setAttribute("aria-pressed", String(on));
  button.textContent = on ? "Unmute" : "Mute";
  $("call-panel").classList.toggle("muted", on);
}

// ---------- ringing for incoming phone calls ----------

const RING_CHOICE = "oscar.ring";
const rungFor = new Set();
let chimeContext = null;

function ringWanted() {
  try {
    return localStorage.getItem(RING_CHOICE) === "yes";
  } catch {
    return false;
  }
}

function showRingHint() {
  const hint = $("phone-ring-hint");
  if (!("Notification" in window)) {
    hint.textContent = "A chime in this tab. This browser can't show notifications.";
  } else if (Notification.permission === "denied") {
    hint.textContent = "A chime in this tab. Notifications are blocked for this site in the browser's settings.";
  } else {
    hint.textContent = "A chime in this tab, and a notification while you are in another one.";
  }
}

function setUpRing() {
  const box = $("phone-ring");
  box.checked = ringWanted();
  showRingHint();
  box.addEventListener("change", async () => {
    try {
      localStorage.setItem(RING_CHOICE, box.checked ? "yes" : "no");
    } catch {
      // Private browsing: on until the tab closes.
    }
    if (box.checked) {
      chime(); // how it sounds, and it lets this tab play sound from now on
      if ("Notification" in window && Notification.permission === "default") {
        await Notification.requestPermission().catch(() => {});
      }
    }
    showRingHint();
  });
}

// Two soft notes, made here: no sound file to fetch.
function chime() {
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return;
    chimeContext = chimeContext || new AudioContextClass();
    chimeContext.resume().catch(() => {});
    const now = chimeContext.currentTime;
    [880, 1175].forEach((frequency, index) => {
      const tone = chimeContext.createOscillator();
      const volume = chimeContext.createGain();
      const start = now + index * 0.18;
      tone.type = "sine";
      tone.frequency.value = frequency;
      volume.gain.setValueAtTime(0.0001, start);
      volume.gain.exponentialRampToValueAtTime(0.2, start + 0.02);
      volume.gain.exponentialRampToValueAtTime(0.0001, start + 0.5);
      tone.connect(volume).connect(chimeContext.destination);
      tone.start(start);
      tone.stop(start + 0.55);
    });
  } catch {
    // A courtesy: the call goes ahead without it.
  }
}

// Once for each incoming call, when it first shows up here.
function ringFor(call) {
  if (!ringWanted() || rungFor.has(call.id)) return;
  rungFor.add(call.id);
  chime();
  if (!document.hidden || !("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const note = new Notification(`Someone is calling ${state.agentName}`, {
      body: `From ${call.from_number || "an unknown number"}. ${state.agentName} is answering.`,
      tag: call.id,
    });
    note.onclick = () => {
      window.focus();
      $("call").scrollIntoView({ block: "start" });
      note.close();
    };
  } catch {
    // Some browsers only allow notifications from a service worker.
  }
}

// ---------- one call: its link, deleting it ----------

// The page's address with this call open, so a reload, a bookmark or a copied
// link opens it again.
function callLink(callId) {
  const url = new URL(location.href);
  url.hash = "";
  url.searchParams.set("call", callId);
  return url.href;
}

function showCallInAddress(callId) {
  const url = new URL(location.href);
  if (callId) url.searchParams.set("call", callId);
  else url.searchParams.delete("call");
  if (url.href !== location.href) window.history.replaceState(window.history.state, "", url);
}

async function copyCallLink() {
  const button = $("detail-link");
  try {
    await navigator.clipboard.writeText(callLink(state.detailCallId));
    button.textContent = "Link copied";
    setTimeout(() => (button.textContent = "Copy link"), 1500);
  } catch {
    button.textContent = "Couldn't copy";
  }
}

function askToDelete() {
  showError("detail-error", "");
  $("detail-confirm").hidden = false;
  $("detail-delete").setAttribute("aria-expanded", "true");
  $("detail-delete-no").focus(); // the safe choice comes first
}

function keepCall() {
  $("detail-confirm").hidden = true;
  $("detail-delete").setAttribute("aria-expanded", "false");
  $("detail-delete").focus();
}

async function deleteCall() {
  const callId = state.detailCallId;
  if (!callId) return;
  const button = $("detail-delete-yes");
  button.disabled = true;
  // Where focus goes afterwards: the next call down the list, else the search box.
  const ids = state.history.ids;
  const next = ids[ids.indexOf(callId) + 1] || ids[ids.indexOf(callId) - 1];
  try {
    await api(`/api/calls/${callId}`, { method: "DELETE" });
    onCallDeleted(callId);
    const nextButton = next && document.querySelector(`button[data-call-id="${CSS.escape(next)}"]`);
    (nextButton || $("history-search")).focus();
  } catch (error) {
    $("detail-confirm").hidden = true;
    showError("detail-error", error.message);
  } finally {
    button.disabled = false;
  }
}

// A call deleted here or in another tab: gone from the list, the side panel and the live panel.
function onCallDeleted(callId) {
  const call = state.calls.get(callId);
  const history = state.history;
  if (hasOpenTodo(call)) history.openTodos = Math.max(0, history.openTodos - 1);
  state.calls.delete(callId);
  history.ids = history.ids.filter((id) => id !== callId);
  if (state.detailCallId === callId) closeDetail({ restoreFocus: false });
  if (state.focusCallId === callId) {
    state.focusCallId = null;
    state.liveSince = null;
    resetTranscript($("live-transcript"), "Nothing yet. Start a call and the transcript appears here.");
    $("live-summary").hidden = true;
    $("call-status").textContent = IDLE_CALL_TEXT;
    $("call-timer").textContent = "";
    updateTitle(null);
    updateCallUi();
  }
  renderCalls();
  refreshSpendingSoon();
}

// How far into the call a line was said ("1:05"), with the time of day on hover.
function lineTime(call, message) {
  const said = Date.parse(message.created_at);
  const began = call ? Date.parse(call.started_at) : NaN;
  if (Number.isNaN(said) || Number.isNaN(began)) return null;
  const time = document.createElement("time");
  time.className = "said-at";
  time.dateTime = message.created_at;
  time.textContent = formatDuration(Math.max(0, Math.floor((said - began) / 1000)));
  time.title = new Date(said).toLocaleTimeString();
  return time;
}

// ---------- spending, day by day ----------

const CHART_DAYS = 14;
const chart = { days: [], active: -1 };

function dayKey(date) {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${String(date.getDate()).padStart(2, "0")}`;
}

function dayName(date) {
  return date.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

function dayDetail(day) {
  const calls = day.calls === 1 ? "1 call" : `${day.calls} calls`;
  return day.calls ? `${formatCost(day.cost)}, ${calls}` : "No cost";
}

// What calls cost on each of the last 14 days, in this computer's time zone.
function renderSpendChart(recent) {
  const today = new Date();
  const days = [];
  for (let back = CHART_DAYS - 1; back >= 0; back--) {
    const date = new Date(today.getFullYear(), today.getMonth(), today.getDate() - back);
    days.push({ key: dayKey(date), date, cost: 0, calls: 0 });
  }
  const byKey = new Map(days.map((day) => [day.key, day]));
  for (const [startedAt, cost] of recent) {
    const when = new Date(startedAt);
    const day = Number.isNaN(when.getTime()) ? null : byKey.get(dayKey(when));
    if (day && typeof cost === "number") {
      day.cost += cost;
      day.calls += 1;
    }
  }
  chart.days = days;
  const total = days.reduce((sum, day) => sum + day.cost, 0);
  const most = Math.max(...days.map((day) => day.cost));
  $("spend-chart").hidden = false;
  $("spend-chart-note").textContent = total
    ? `${formatCost(total)} over the last ${CHART_DAYS} days.`
    : `No call has cost anything in the last ${CHART_DAYS} days.`;
  const bars = $("spend-bars");
  bars.hidden = !total;
  const tip = document.createElement("div");
  tip.className = "bar-tip";
  tip.hidden = true;
  tip.setAttribute("aria-hidden", "true");
  const slots = days.map((day, index) => {
    const slot = document.createElement("div");
    slot.className = "bar-slot";
    slot.dataset.index = String(index);
    const plot = document.createElement("div");
    plot.className = "bar-plot";
    if (day.cost > 0 && day.cost === most) {
      const value = document.createElement("span");
      value.className = "bar-value";
      value.textContent = formatCost(day.cost);
      plot.append(value);
    }
    const bar = document.createElement("span");
    bar.className = "bar";
    // At least 2px, so a day that cost a little still shows.
    bar.style.height = day.cost > 0 ? `max(2px, ${((day.cost / most) * 100).toFixed(2)}%)` : "0";
    plot.append(bar);
    const label = document.createElement("span");
    label.className = "bar-day";
    label.textContent = index === CHART_DAYS - 1 ? "Today" : String(day.date.getDate());
    slot.append(plot, label);
    slot.setAttribute("aria-hidden", "true");
    return slot;
  });
  bars.replaceChildren(...slots, tip);
  chart.active = -1;
  $("spend-table").tBodies[0].replaceChildren(
    ...days.map((day) => {
      const row = document.createElement("tr");
      for (const text of [dayName(day.date), day.calls ? formatCost(day.cost) : "$0", String(day.calls)]) {
        const cell = document.createElement("td");
        cell.textContent = text;
        row.append(cell);
      }
      return row;
    }),
  );
}

// Shows one day's figures over its bar (from the pointer or the arrow keys).
function showDay(index, { announce = false } = {}) {
  const bars = $("spend-bars");
  const tip = bars.querySelector(".bar-tip");
  const slots = bars.querySelectorAll(".bar-slot");
  for (const slot of slots) slot.classList.toggle("active", Number(slot.dataset.index) === index);
  chart.active = index;
  const day = chart.days[index];
  if (!day || !tip) {
    if (tip) tip.hidden = true;
    return;
  }
  tip.replaceChildren();
  const when = document.createElement("strong");
  when.textContent = dayName(day.date);
  tip.append(when, document.createElement("br"), dayDetail(day));
  tip.hidden = false;
  // Just above the day's bar, kept inside the chart at either end.
  const slot = slots[index];
  const middle = slot.offsetLeft + slot.offsetWidth / 2;
  const half = tip.offsetWidth / 2;
  tip.style.left = `${Math.min(Math.max(middle, half), bars.clientWidth - half)}px`;
  const barTop = slot.querySelector(".bar").getBoundingClientRect().top - bars.getBoundingClientRect().top;
  tip.style.top = `${Math.max(barTop - 8, 0)}px`;
  if (announce) $("spend-chart-live").textContent = `${dayName(day.date)}: ${dayDetail(day)}`;
}

function setUpSpendChart() {
  const bars = $("spend-bars");
  bars.addEventListener("pointermove", (event) => {
    const slot = event.target.closest(".bar-slot");
    if (slot && Number(slot.dataset.index) !== chart.active) showDay(Number(slot.dataset.index));
  });
  bars.addEventListener("pointerleave", () => {
    if (document.activeElement !== bars) showDay(-1);
  });
  bars.addEventListener("focus", () => showDay(chart.active >= 0 ? chart.active : CHART_DAYS - 1, { announce: true }));
  bars.addEventListener("blur", () => showDay(-1));
  bars.addEventListener("keydown", (event) => {
    const moves = { ArrowLeft: chart.active - 1, ArrowRight: chart.active + 1, Home: 0, End: CHART_DAYS - 1 };
    if (!(event.key in moves)) return;
    event.preventDefault();
    // Nothing shown yet (focus came without its event): start from today.
    const to = chart.active < 0 ? CHART_DAYS - 1 : moves[event.key];
    showDay(Math.min(Math.max(to, 0), CHART_DAYS - 1), { announce: true });
  });
}

// ---------- OpenRouter's models, as suggestions ----------

async function loadModelSuggestions() {
  let models;
  try {
    ({ models } = await api("/api/models"));
  } catch {
    return; // the page's own few suggestions stay
  }
  if (!Array.isArray(models) || !models.length) return;
  $("model-suggestions").replaceChildren(
    ...models.map((model) => {
      const option = document.createElement("option");
      option.value = model.id;
      option.label = model.name;
      return option;
    }),
  );
  $("agent-model-hint").textContent =
    `Writes Oscar's WhatsApp replies. Type to pick from OpenRouter's ${models.length} chat models. ` +
    "Calls always use OpenAI's gpt-audio, which hears and speaks in one step.";
}

// ---------- start ----------

async function init() {
  revealOnScroll();
  setUpMenu();
  setUpCounters();
  setUpHistory();
  $("agent-form").addEventListener("input", updateAgentDirty);
  $("agent-form").addEventListener("change", updateAgentDirty);
  $("agent-accent").addEventListener("change", showAccentHint);
  $("agent-voice").addEventListener("change", showAccentHint);
  $("agent-accent-hint").addEventListener("click", useVoice);
  $("agent-discard").addEventListener("click", () => {
    if (state.agentSettings) fillAgentForm(state.agentSettings);
    showError("agent-error", "");
    $("agent-saved").textContent = ""; // "Added a file… Save changes to keep it." no longer holds
  });
  $("agent-preview").addEventListener("click", previewVoice);
  $("detail-backdrop").addEventListener("click", () => closeDetail());
  $("detail-callback").addEventListener("click", callBack);
  $("detail-copy").addEventListener("click", copyNumber);
  $("detail-link").addEventListener("click", copyCallLink);
  $("detail-delete").addEventListener("click", askToDelete);
  $("detail-delete-yes").addEventListener("click", deleteCall);
  $("detail-delete-no").addEventListener("click", keepCall);
  $("call-mute").addEventListener("click", () => setMuted(!muted));
  $("call-mic").addEventListener("change", onMicrophoneChosen);
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener("devicechange", listMicrophones);
  }
  listMicrophones();
  setUpRing();
  setUpSpendChart();
  loadModelSuggestions();
  // Unsaved settings are not lost to a closed tab or a reload without a warning.
  window.addEventListener("beforeunload", (event) => {
    if (unsavedChanges()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  $("connection-reload").addEventListener("click", () => location.reload());
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
  $("notify-form").addEventListener("submit", saveNotify);
  $("notify-test").addEventListener("click", testNotify);
  // Ends this tab's browser call when the page really goes away. Not on
  // beforeunload: that also fires when the "Leave site?" warning is then
  // answered with Stay, and the call would end anyway.
  window.addEventListener("pagehide", () => {
    if (state.browserCallId) {
      navigator.sendBeacon(`/api/calls/${state.browserCallId}/end`);
    }
  });
  setInterval(tickTimer, 1000);

  try {
    // Status first: it fills the voice and accent lists, which the agent form then selects from.
    await loadStatus();
    await Promise.all([loadAgent(), loadCalls(), loadWhatsApp(), loadSpending(), loadNotify()]);
    // A link to one call (?call=…) opens it.
    const linked = new URL(location.href).searchParams.get("call");
    if (linked) openDetail(linked);
  } catch (error) {
    const notice = $("setup-notice");
    notice.textContent = `Could not reach the server: ${error.message}`;
    notice.hidden = false;
    updateCallUi(); // keeps the call buttons off until the server answers
  }
  connectEvents();
  pollWhatsApp();
  pollDashboard();
}

init();
