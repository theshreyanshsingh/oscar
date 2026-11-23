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
  max_call_minutes: "Longest call",
  transfer_number: "Put callers through to",
  voicemail_message: "Voicemail message",
  name_of_list: "Name",
  contacts_csv: "People to call",
  country_code: "Country code",
  calling_from: "Calling from",
  calling_until: "Calling until",
  timezone: "Time zone",
  at_once: "Calls at once",
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

// Signed out (the session ended, or the password changed), or Oscar is locked
// until it has a password: the sign-in page says which, and brings this page back.
let goingToSignIn = false;

function needsSignIn(status, body) {
  return status === 401 || (status === 503 && Boolean(body && body.locked));
}

function goToSignIn() {
  if (goingToSignIn) return;
  goingToSignIn = true;
  const here = location.pathname + location.search + location.hash;
  location.assign(here === "/" ? "/login" : `/login?next=${encodeURIComponent(here)}`);
}

async function api(path, options = {}) {
  // A file goes as a form, whose boundary the browser writes into its own Content-Type.
  const response = await fetch(path, {
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
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
  if (needsSignIn(response.status, body)) {
    goToSignIn();
    throw new Error("Sign in to Oscar again.");
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
  if (fill) {
    $("notify-url").value = webhook.url;
    $("notify-alerts").checked = webhook.alerts !== false;
  }
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

// ---------- blocked numbers ----------

async function loadBlocked() {
  const { numbers } = await api("/api/blocked-numbers");
  state.blocked = numbers;
  $("blocked-numbers").value = numbers.join("\n");
}

function blockedCount(numbers) {
  return numbers.length === 1 ? "1 number is blocked." : `${numbers.length} numbers are blocked.`;
}

async function saveBlocked(event) {
  event.preventDefault();
  showError("blocked-error", "");
  const button = $("blocked-save");
  button.disabled = true;
  try {
    const { numbers } = await api("/api/blocked-numbers", {
      method: "PUT",
      body: JSON.stringify({ numbers: $("blocked-numbers").value.split("\n") }),
    });
    state.blocked = numbers;
    $("blocked-numbers").value = numbers.join("\n");
    $("blocked-status").textContent = `Saved. ${blockedCount(numbers)}`;
    renderDetailActions(state.calls.get(state.detailCallId));
  } catch (error) {
    showError("blocked-error", error.message);
  } finally {
    button.disabled = false;
  }
}

function digitsOf(number) {
  return String(number || "").replace(/\D/g, "").replace(/^0+/, "");
}

// Whether a caller's number is on the list (the server matches the same way: app/phone.py).
function isBlocked(number) {
  const digits = digitsOf(number);
  return Boolean(digits) && (state.blocked || []).some((entry) => {
    const wanted = digitsOf(entry);
    return wanted.length >= 8 && digits.endsWith(wanted);
  });
}

// "Block number" in an incoming call's panel: adds it to the list.
async function blockCaller() {
  const call = state.calls.get(state.detailCallId);
  if (!call || !call.from_number) return;
  const button = $("detail-block");
  button.disabled = true;
  const box = $("blocked-numbers");
  // What is typed in Blocked numbers and not saved yet is left as it is.
  const untouched = box.value === (state.blocked || []).join("\n");
  try {
    // The list as it is now (another tab may have changed it): the server keeps only what is sent.
    const { numbers: blocked } = await api("/api/blocked-numbers");
    const { numbers } = await api("/api/blocked-numbers", {
      method: "PUT",
      body: JSON.stringify({ numbers: [...blocked, call.from_number] }),
    });
    state.blocked = numbers;
    if (untouched) box.value = numbers.join("\n");
    $("blocked-status").textContent = blockedCount(numbers);
  } catch (error) {
    showError("detail-error", error.message);
  }
  renderDetailActions(call); // "Number blocked", and off; or back on after a failure
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
      body: JSON.stringify({ url: $("notify-url").value, alerts: $("notify-alerts").checked }),
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

  // Sign out, when the dashboard asks for a password (app/signin.py).
  $("sign-out").hidden = !(status.dashboard && status.dashboard.password);

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
  showListsAvailability();
  $("blocked-panel").hidden = !status.phone.enabled;
  $("phone-setup").hidden = status.phone.enabled;
  if (status.phone.enabled) {
    $("phone-number").textContent = status.phone.number || "no number yet";
    $("phone-webhook").textContent = status.phone.voice_webhook;
    // Twilio is asked once (and again on a change), not on every refresh of the status.
    if (!phoneNumbers.loaded && !phoneNumbers.loading) {
      phoneNumbers.loading = true;
      loadNumbers().finally(() => (phoneNumbers.loading = false));
    }
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
  "agent-max-minutes",
  "agent-transfer",
  "agent-voicemail",
  "agent-record",
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
  $("agent-try").hidden = !dirty;
  if (dirty) $("agent-saved").textContent = "";
}

// "Try in a call": a browser call with the settings in the form, saved or not.
function tryUnsavedSettings() {
  const form = $("agent-form");
  if (!form.reportValidity()) return;
  if (state.callUi !== "idle") {
    showError("agent-error", "Finish the call you are on first.");
    return;
  }
  showError("agent-error", "");
  $("call").scrollIntoView({ behavior: "smooth", block: "start" });
  startBrowserCall({ agent: agentFormBody() });
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
    max_call_minutes: Number($("agent-max-minutes").value),
    transfer_number: $("agent-transfer").value,
    voicemail_message: $("agent-voicemail").value,
    record_calls: $("agent-record").checked,
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
      if (needsSignIn(response.status, body)) {
        goToSignIn();
        throw new Error("Sign in to Oscar again.");
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
  $("agent-max-minutes").value = String(agent.max_call_minutes || 30);
  $("agent-transfer").value = agent.transfer_number || "";
  $("agent-voicemail").value = agent.voicemail_message || "";
  $("agent-record").checked = agent.record_calls !== false;
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
  for (const chip of document.querySelectorAll(".chip[data-show]")) {
    chip.addEventListener("click", () => {
      // Only the history's own filters: Get a new number has chips of its own.
      for (const other of chip.parentElement.querySelectorAll(".chip")) {
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
  const tags = outcomeTags(call);
  if (tags.length) {
    if (cell.textContent === "—") cell.textContent = "";
    const line = document.createElement("span");
    line.className = "call-tags";
    for (const tag of tags) {
      const chip = document.createElement("span");
      chip.className = "call-tag";
      chip.textContent = tag;
      line.append(chip);
    }
    cell.append(line);
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
    ...$("call-detail").querySelectorAll("button, a[href], input, textarea, summary, audio[controls], [tabindex='0']"),
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
  // An incoming call's number, when phone calls are set up: block it, or say it is.
  const block = $("detail-block");
  const caller = call && call.kind === "phone_inbound" ? call.from_number : "";
  block.hidden = !caller || !phoneReady;
  const blocked = Boolean(caller) && isBlocked(caller);
  block.textContent = blocked ? "Number blocked" : "Block number";
  block.disabled = blocked;
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
  // Focus goes back here on closing: the history table's Transcript or a list's Open call.
  const opener = document.activeElement;
  state.detailOpener = opener && opener !== document.body && !$("call-detail").contains(opener) ? opener : state.detailOpener;
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
  renderDetailRecording(null);
  renderDetailOutcome(null);
  const container = $("detail-transcript");
  resetTranscript(container, "Loading…");
  renderCalls();
  try {
    const { call, messages, searches } = await api(`/api/calls/${callId}`);
    if (state.detailCallId !== callId) return;
    // Call back, Copy number and Block read it from here, for a call not on the history page too.
    state.calls.set(call.id, call);
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
    renderDetailRecording(call);
    renderDetailOutcome(call);
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
  $("detail-audio").pause(); // a recording playing must not go on behind the closed panel
  renderCalls();
  const opener = state.detailOpener;
  state.detailOpener = null;
  // Back to the button that opened it, or the one drawn in its place since.
  if (restoreFocus && callId) {
    const again = opener && opener.dataset && opener.dataset.key
      ? document.querySelector(`[data-key="${CSS.escape(opener.dataset.key)}"]`)
      : null;
    const target = (opener && opener.isConnected ? opener : again) ||
      document.querySelector(`#calls-body button[data-call-id="${callId}"]`);
    if (target) target.focus({ preventScroll: target !== opener && target !== again });
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
    // The recording is ready a moment after the call ends.
    renderDetailRecording(call);
    renderDetailOutcome(call);
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
    loadLists().catch(() => {});
    refreshDashboard();
    if (state.focusCallId) focusCall(state.focusCallId);
  };
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) {
      // The browser gave up: Oscar refused the stream (signed out: loadStatus then
      // goes to the sign-in page), or a sign-in in front of Oscar expired.
      showConnectionNotice("Lost the connection to Oscar.", { reload: true });
      loadStatus().catch(() => {});
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
    else if (data.type === "campaign") onListChanged(data.campaign);
    else if (data.type === "campaign_deleted") onListDeleted(data.id);
    else if (data.type === "do_not_call") loadLists().catch(() => {});
    else if (data.type === "numbers") loadNumbers().catch(() => {});
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
  $("agent-try").disabled = state.callUi !== "idle" || !ready;
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

// `agent`: settings to try on this call only ("Try in a call"), instead of the saved ones.
async function startBrowserCall({ agent = null } = {}) {
  showError("call-error", "");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.RTCPeerConnection) {
    showError(
      "call-error",
      "This page can't use your microphone. Open Oscar at http://localhost (or over https) instead.",
    );
    return;
  }
  state.callUi = "starting";
  $("call-trying").hidden = !agent;
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
        ...(agent ? { agent } : {}),
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
// records it on the call (the technical reason goes to its log only), and that
// line replaces this message as soon as it arrives.
function audioNeverConnected() {
  if (!peer || !state.browserCallId) return; // that call is over already
  showError("call-error", "The call's audio couldn't connect. Try again, or call from another network.");
  endBrowserCall("audio_failed");
}

function cleanupBrowserCall() {
  $("call-trying").hidden = true;
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

// Signing out ends this tab's browser call first (the call would lose the
// dashboard anyway). Unsaved settings still ask before the page goes.
function setUpSignOut() {
  const form = $("sign-out");
  form.addEventListener("submit", async (event) => {
    if (!state.browserCallId) return;
    event.preventDefault();
    await endBrowserCall();
    form.submit();
  });
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


// ---------- call lists (app/campaigns.py) ----------

// Where each number of a list has got to, in the order the bar shows them.
const PERSON_STAGES = [
  ["answered", "answered"],
  ["voicemail", "voicemail"],
  ["no_answer", "no answer"],
  ["busy", "busy"],
  ["failed", "failed"],
  ["skipped", "skipped"],
  ["calling", "calling now"],
  ["waiting", "to call"],
];
const STAGE_LABELS = {
  waiting: "To call",
  calling: "Calling",
  answered: "Answered",
  voicemail: "Voicemail",
  no_answer: "No answer",
  busy: "Busy",
  failed: "Failed",
  skipped: "Skipped",
};
const CALL_AGAIN_STAGES = ["voicemail", "no_answer", "busy", "failed"];
const LIST_PREVIEW_DELAY_MS = 400;
const DROP_NOTE = "or drop one here: a row for each person, with a column of phone numbers";

const lists = {
  byId: new Map(),
  people: new Map(), // list id -> its numbers, for the lists showing them
  open: new Set(), // lists showing their numbers
  confirming: null, // the list asking "Delete?"
  stopping: null, // the list asking "Stop?"
  creating: false, // a new list is being made: both of its buttons wait
  csv: "", // the file chosen (or rows pasted) for a new list
  fileName: "",
  workbook: null, // the Excel file chosen, to read another of its sheets
  fileAsked: 0, // files (and sheets) read: one that finishes after a newer choice is dropped
  previewTimer: null,
  previewAsked: 0, // previews asked for: an older one that answers late is not shown
  peopleTimers: new Map(),
  doNotCall: [],
};

async function loadLists() {
  const data = await api("/api/campaigns");
  lists.byId = new Map(data.campaigns.map((campaign) => [campaign.id, campaign]));
  lists.doNotCall = data.do_not_call;
  renderLists();
  renderDoNotCall();
  for (const id of lists.open) loadPeople(id);
}

function showListsAvailability() {
  const phone = Boolean(state.status && state.status.phone.enabled);
  $("lists-off").hidden = phone;
  $("list-start").disabled = !phone || lists.creating;
}

function clockRange(campaign) {
  const from = typeof campaign.calling_from === "number" ? minutesToClock(campaign.calling_from) : campaign.calling_from;
  const until = typeof campaign.calling_until === "number" ? minutesToClock(campaign.calling_until) : campaign.calling_until;
  return `${from}–${until}`;
}

function minutesToClock(minutes) {
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

function listState(campaign) {
  if (campaign.status === "running") {
    if ((campaign.status_detail || "").startsWith("Waiting")) return ["waiting", "Waiting for its hours"];
    return ["running", (campaign.counts.calling || 0) > 0 ? "Calling" : "Running"];
  }
  return {
    paused: ["paused", "Paused"],
    done: ["done", "Done"],
    stopped: ["stopped", "Stopped"],
  }[campaign.status] || ["stopped", campaign.status];
}

function renderLists() {
  const container = $("lists-cards");
  const campaigns = [...lists.byId.values()].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  keepFocus(container, () => {
    container.replaceChildren();
    if (!campaigns.length) {
      const empty = document.createElement("div");
      empty.className = "lists-empty panel panel-outline";
      const title = document.createElement("p");
      title.className = "panel-title";
      title.textContent = "No call lists yet";
      const hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent =
        "A list you start shows here as Oscar works through it: who answered, who got a voicemail, and a link to every call.";
      empty.append(title, hint);
      container.append(empty);
      return;
    }
    for (const campaign of campaigns) container.append(listCard(campaign));
  });
}

function listCard(campaign) {
  const card = document.createElement("article");
  card.className = "list-card panel panel-outline";
  card.dataset.listId = campaign.id;
  const counts = campaign.counts || {};
  const total = campaign.total || 0;

  const head = document.createElement("header");
  head.className = "list-head";
  const titles = document.createElement("div");
  const name = document.createElement("h4");
  name.className = "list-name";
  name.textContent = campaign.name;
  const meta = document.createElement("p");
  meta.className = "list-meta";
  const people = `${total} ${total === 1 ? "person" : "people"}`;
  meta.textContent = `${people} · ${clockRange(campaign)} ${campaign.timezone.replace(/_/g, " ")} · ${campaign.at_once} at once`;
  titles.append(name, meta);
  const [stateKey, stateLabel] = listState(campaign);
  const badge = document.createElement("span");
  badge.className = `list-state state-${stateKey}`;
  badge.textContent = stateLabel;
  head.append(titles, badge);

  // How far through the list Oscar is, a segment for each stage.
  const done = total - (counts.waiting || 0) - (counts.calling || 0);
  const bar = document.createElement("div");
  bar.className = "list-bar";
  bar.setAttribute("role", "progressbar");
  bar.setAttribute("aria-valuemin", "0");
  bar.setAttribute("aria-valuemax", String(total));
  bar.setAttribute("aria-valuenow", String(done));
  bar.setAttribute("aria-label", `${campaign.name}: ${done} of ${total} called`);
  for (const [stage] of PERSON_STAGES) {
    if (!counts[stage]) continue;
    const segment = document.createElement("span");
    segment.className = `seg stage-${stage}`;
    segment.style.flexGrow = String(counts[stage]);
    bar.append(segment);
  }

  const tally = document.createElement("ul");
  tally.className = "list-tally";
  for (const [stage, label] of PERSON_STAGES) {
    if (!counts[stage]) continue;
    const item = document.createElement("li");
    const dot = document.createElement("i");
    dot.className = `dot stage-${stage}`;
    dot.setAttribute("aria-hidden", "true");
    const figure = document.createElement("strong");
    figure.textContent = String(counts[stage]);
    item.append(dot, figure, document.createTextNode(` ${label}`));
    tally.append(item);
  }

  card.append(head, bar, tally);
  if (campaign.status_detail) {
    const detail = document.createElement("p");
    detail.className = "list-detail";
    detail.textContent = campaign.status_detail;
    card.append(detail);
  }

  const actions = document.createElement("div");
  actions.className = "list-actions";
  const button = (label, key, handler, className = "link") => {
    const element = document.createElement("button");
    element.type = "button";
    element.className = className;
    element.textContent = label;
    element.dataset.key = `${key}-${campaign.id}`;
    element.addEventListener("click", handler);
    actions.append(element);
    return element;
  };
  if (campaign.status === "running") {
    button("Pause", "pause", () => listAction(campaign.id, "pause"), "secondary");
  } else if (campaign.status === "paused" || campaign.status === "stopped") {
    const resume = button("Resume", "resume", () => listAction(campaign.id, "resume"), "secondary");
    resume.disabled = !(state.status && state.status.phone.enabled);
  }
  const again = CALL_AGAIN_STAGES.reduce((sum, stage) => sum + (counts[stage] || 0), 0);
  if (again && campaign.status !== "stopped") {
    button(`Call ${again} again`, "again", () => listAction(campaign.id, "call-again"));
  }
  const showing = lists.open.has(campaign.id);
  const toggle = button(showing ? "Hide people" : "Show people", "people", () => togglePeople(campaign.id));
  toggle.setAttribute("aria-expanded", String(showing));
  if (campaign.status === "running" || campaign.status === "paused") {
    button("Stop", "stop", () => {
      lists.stopping = campaign.id;
      renderLists();
      const yes = document.querySelector(`[data-key="stop-yes-${CSS.escape(campaign.id)}"]`);
      if (yes) yes.focus();
    });
  }
  if (!counts.calling) {
    const remove = button("Delete", "delete", () => {
      lists.confirming = campaign.id;
      renderLists();
      const yes = document.querySelector(`[data-key="delete-yes-${CSS.escape(campaign.id)}"]`);
      if (yes) yes.focus();
    });
    remove.classList.add("danger");
  }
  card.append(actions);

  if (lists.confirming === campaign.id) card.append(deleteListConfirm(campaign));
  if (lists.stopping === campaign.id) card.append(stopListConfirm(campaign));
  const error = document.createElement("p");
  error.className = "error";
  error.id = `list-error-${campaign.id}`;
  error.setAttribute("role", "alert");
  error.hidden = true;
  card.append(error);
  if (showing) card.append(peopleTable(campaign));
  return card;
}

function deleteListConfirm(campaign) {
  const box = document.createElement("div");
  box.className = "detail-confirm";
  box.setAttribute("role", "alertdialog");
  const text = document.createElement("p");
  text.textContent = `Delete "${campaign.name}" and its numbers? The calls it made stay in Call history.`;
  const row = document.createElement("div");
  row.className = "actions";
  const yes = document.createElement("button");
  yes.type = "button";
  yes.className = "danger-button";
  yes.textContent = "Delete list";
  yes.dataset.key = `delete-yes-${campaign.id}`;
  yes.addEventListener("click", () => deleteList(campaign.id));
  const no = document.createElement("button");
  no.type = "button";
  no.className = "link";
  no.textContent = "Keep it";
  no.addEventListener("click", () => {
    lists.confirming = null;
    renderLists();
  });
  row.append(yes, no);
  box.append(text, row);
  return box;
}

function stopListConfirm(campaign) {
  const box = document.createElement("div");
  box.className = "detail-confirm";
  box.setAttribute("role", "alertdialog");
  box.setAttribute("aria-label", `Stop ${campaign.name}`);
  const text = document.createElement("p");
  text.textContent =
    `Stop calling from "${campaign.name}"? Calls going on now carry on to their end. ` +
    "The people not called yet stay on the list, and Resume calls them later.";
  const row = document.createElement("div");
  row.className = "actions";
  const back = () => {
    lists.stopping = null;
    renderLists();
    const stop = document.querySelector(`[data-key="stop-${CSS.escape(campaign.id)}"]`);
    if (stop) stop.focus();
  };
  const yes = document.createElement("button");
  yes.type = "button";
  yes.className = "danger-button";
  yes.textContent = "Stop list";
  yes.dataset.key = `stop-yes-${campaign.id}`;
  yes.addEventListener("click", () => {
    lists.stopping = null;
    listAction(campaign.id, "stop");
  });
  const no = document.createElement("button");
  no.type = "button";
  no.className = "link";
  no.textContent = "Keep calling";
  no.addEventListener("click", back);
  box.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      back();
    }
  });
  row.append(yes, no);
  box.append(text, row);
  return box;
}

// Who a person on a list is: their name when a column is headed for it, else the first column.
function personLabel(fields) {
  const named = Object.keys(fields).find((heading) => /name/i.test(heading) && fields[heading]);
  return named ? fields[named] : Object.values(fields)[0] || "";
}

function peopleTable(campaign) {
  const wrap = document.createElement("div");
  wrap.className = "list-people table-wrap";
  const people = lists.people.get(campaign.id);
  if (!people) {
    const loading = document.createElement("p");
    loading.className = "hint";
    loading.textContent = "Loading…";
    wrap.append(loading);
    return wrap;
  }
  const table = document.createElement("table");
  const head = document.createElement("thead");
  head.innerHTML = '<tr><th scope="col">Person</th><th scope="col">Number</th><th scope="col">Result</th><th scope="col"><span class="visually-hidden">Call</span></th></tr>';
  const body = document.createElement("tbody");
  for (const person of people) {
    const row = document.createElement("tr");
    const who = document.createElement("td");
    who.dataset.label = "Person";
    const label = personLabel(person.fields || {});
    who.textContent = label || "—";
    const number = document.createElement("td");
    number.dataset.label = "Number";
    number.className = "number";
    number.textContent = person.number;
    const result = document.createElement("td");
    result.dataset.label = "Result";
    result.className = "wrap";
    const chip = document.createElement("span");
    chip.className = `stage-chip stage-${person.status}`;
    chip.textContent = STAGE_LABELS[person.status] || person.status;
    result.append(chip);
    if (person.detail) {
      const detail = document.createElement("span");
      detail.className = "stage-detail";
      detail.textContent = person.detail;
      result.append(detail);
    }
    const open = document.createElement("td");
    open.dataset.label = "";
    if (person.call_id) {
      const link = document.createElement("button");
      link.type = "button";
      link.className = "link";
      link.textContent = "Open call";
      link.dataset.key = `open-${person.id}`;
      link.setAttribute("aria-label", `Open the call to ${label || person.number}`);
      link.addEventListener("click", () => openDetail(person.call_id));
      open.append(link);
    }
    row.append(who, number, result, open);
    body.append(row);
  }
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

async function loadPeople(id) {
  try {
    const { campaign, contacts } = await api(`/api/campaigns/${id}`);
    lists.people.set(id, contacts);
    lists.byId.set(id, campaign);
    renderLists();
  } catch (error) {
    showError(`list-error-${id}`, error.message);
  }
}

// A list's numbers change with each call: read them again, at most once a second.
function refreshPeopleSoon(id) {
  if (!lists.open.has(id) || lists.peopleTimers.has(id)) return;
  lists.peopleTimers.set(
    id,
    setTimeout(() => {
      lists.peopleTimers.delete(id);
      loadPeople(id);
    }, 1000),
  );
}

function togglePeople(id) {
  if (lists.open.has(id)) {
    lists.open.delete(id);
    renderLists();
    return;
  }
  lists.open.add(id);
  renderLists();
  loadPeople(id);
}

async function listAction(id, action) {
  try {
    const { campaign } = await api(`/api/campaigns/${id}/${action}`, { method: "POST" });
    lists.byId.set(id, campaign);
    renderLists();
    refreshPeopleSoon(id);
  } catch (error) {
    showError(`list-error-${id}`, error.message);
  }
}

async function deleteList(id) {
  try {
    await api(`/api/campaigns/${id}`, { method: "DELETE" });
    onListDeleted(id);
  } catch (error) {
    lists.confirming = null;
    renderLists();
    showError(`list-error-${id}`, error.message);
  }
}

function onListChanged(campaign) {
  const known = lists.byId.get(campaign.id);
  lists.byId.set(campaign.id, { ...known, ...campaign });
  renderLists();
  refreshPeopleSoon(campaign.id);
}

function onListDeleted(id) {
  lists.byId.delete(id);
  lists.people.delete(id);
  lists.open.delete(id);
  if (lists.confirming === id) lists.confirming = null;
  renderLists();
}

// ----- a new list -----

function fillTimeZones() {
  const select = $("list-timezone");
  const own = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  let zones = [];
  try {
    zones = Intl.supportedValuesOf("timeZone");
  } catch {
    zones = [];
  }
  if (!zones.includes(own)) zones.unshift(own);
  if (!zones.includes("UTC")) zones.push("UTC");
  for (const zone of zones) {
    const option = document.createElement("option");
    option.value = zone;
    option.textContent = zone.replace(/_/g, " ");
    select.append(option);
  }
  select.value = own;
}

function listTemplates() {
  return [$("list-instructions").value, $("list-greeting").value, $("list-voicemail").value];
}

function previewListSoon() {
  clearTimeout(lists.previewTimer);
  lists.previewTimer = setTimeout(previewList, LIST_PREVIEW_DELAY_MS);
}

async function previewList() {
  const box = $("list-preview");
  const asked = ++lists.previewAsked;
  if (!lists.csv.trim()) {
    box.hidden = true;
    return;
  }
  try {
    const preview = await api("/api/campaigns/preview", {
      method: "POST",
      body: JSON.stringify({
        contacts_csv: lists.csv,
        country_code: $("list-country").value,
        templates: listTemplates(),
      }),
    });
    if (asked !== lists.previewAsked) return;
    renderPreview(preview);
  } catch (error) {
    if (asked !== lists.previewAsked) return;
    box.hidden = false;
    box.className = "list-preview bad";
    box.textContent = error.message;
  }
}

function renderPreview(preview) {
  const box = $("list-preview");
  box.replaceChildren();
  box.hidden = false;
  box.className = `list-preview ${preview.count ? "good" : "bad"}`;
  const headline = document.createElement("p");
  headline.className = "preview-headline";
  headline.textContent = preview.count
    ? `${preview.count} ${preview.count === 1 ? "person" : "people"} to call`
    : "No one Oscar can call yet";
  box.append(headline);
  const columns = preview.headings.filter((heading) => preview.sample.some((person) => heading in person.fields));
  if (columns.length) {
    const known = document.createElement("p");
    known.className = "hint";
    known.textContent = `Oscar is told each person's ${columns.join(", ")}. Use them as ${columns
      .slice(0, 2)
      .map((heading) => `{{${heading.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "")}}}`)
      .join(" or ")}.`;
    box.append(known);
  }
  if (preview.unknown_placeholders.length) {
    const unknown = document.createElement("p");
    unknown.className = "preview-warning";
    const names = preview.unknown_placeholders.map((name) => `{{${name}}}`).join(", ");
    unknown.textContent = `${names} ${preview.unknown_placeholders.length === 1 ? "is not a column" : "are not columns"} in the file, so ${preview.unknown_placeholders.length === 1 ? "it" : "they"} would be left blank.`;
    box.append(unknown);
  }
  if (preview.problems.length) {
    const list = document.createElement("ul");
    list.className = "preview-problems";
    for (const problem of preview.problems.slice(0, 5)) {
      const item = document.createElement("li");
      item.textContent = problem;
      list.append(item);
    }
    const more = preview.problems.length - 5 + preview.more_problems;
    if (more > 0) {
      const item = document.createElement("li");
      item.textContent = `And ${more} more row${more === 1 ? "" : "s"} left out.`;
      list.append(item);
    }
    box.append(list);
  }
}

// Spreadsheets go to Oscar to be read (app/sheets.py); it says how to save the ones it cannot read.
const SPREADSHEET_FILE = /\.(xlsx|xlsm|xls|xlsb|ods|numbers)$/i;

async function readWorkbook(file, sheet = "") {
  const form = new FormData();
  form.append("file", file);
  if (sheet) form.append("sheet", sheet);
  return api("/api/campaigns/sheet", { method: "POST", body: form });
}

function showSheets(sheets, chosen) {
  const select = $("list-sheet");
  select.replaceChildren(
    ...sheets.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      return option;
    }),
  );
  select.value = chosen;
  $("list-sheet-row").hidden = sheets.length < 2;
}

function useListRows(text) {
  lists.csv = text;
  $("list-paste").value = text;
  return previewList();
}

async function chooseListFile(file) {
  if (!file) return;
  const spreadsheet = SPREADSHEET_FILE.test(file.name);
  if (file.size > (spreadsheet ? 10_000_000 : 2_000_000)) {
    showError(
      "list-error",
      spreadsheet
        ? "That workbook is over 10 MB. Keep just the sheet of people to call, or split it."
        : "That file is over 2 MB. Split it into smaller lists.",
    );
    return;
  }
  showError("list-error", "");
  const asked = ++lists.fileAsked;
  let text;
  let read = null;
  if (spreadsheet) {
    $("list-file-name").textContent = `Reading ${file.name}…`;
    try {
      read = await readWorkbook(file);
    } catch (error) {
      if (asked !== lists.fileAsked) return;
      $("list-file-name").textContent = lists.fileName || "Choose an Excel or CSV file";
      showError("list-error", error.message);
      return;
    }
    text = read.csv;
  } else {
    text = await file.text();
  }
  if (asked !== lists.fileAsked) return;
  lists.fileName = file.name;
  lists.workbook = read ? file : null;
  showSheets(read ? read.sheets : [], read ? read.sheet : "");
  $("list-file-name").textContent = file.name;
  $("list-drop-note").textContent = "Choose another file, or drop one here";
  $("list-drop").classList.add("chosen");
  if (!$("list-name").value.trim()) {
    const name = file.name.replace(/\.(csv|txt|xlsx|xlsm)$/i, "").replace(/[_-]+/g, " ").trim();
    $("list-name").value = name.charAt(0).toUpperCase() + name.slice(1);
  }
  await useListRows(text);
}

async function chooseSheet(sheet) {
  const file = lists.workbook;
  if (!file) return;
  const asked = ++lists.fileAsked;
  const select = $("list-sheet");
  select.disabled = true;
  showError("list-error", "");
  try {
    const read = await readWorkbook(file, sheet);
    if (asked !== lists.fileAsked) return;
    await useListRows(read.csv);
  } catch (error) {
    if (asked !== lists.fileAsked) return;
    showError("list-error", error.message);
  } finally {
    if (asked === lists.fileAsked) select.disabled = false;
  }
}

function setUpListForm() {
  fillTimeZones();
  const drop = $("list-drop");
  $("list-file").addEventListener("change", (event) => chooseListFile(event.target.files[0]));
  $("list-sheet").addEventListener("change", (event) => chooseSheet(event.target.value));
  for (const type of ["dragenter", "dragover"]) {
    drop.addEventListener(type, (event) => {
      event.preventDefault();
      drop.classList.add("over");
    });
  }
  for (const type of ["dragleave", "drop"]) {
    drop.addEventListener(type, () => drop.classList.remove("over"));
  }
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    chooseListFile(event.dataTransfer.files[0]);
  });
  $("list-paste-toggle").addEventListener("click", () => {
    const paste = $("list-paste");
    paste.hidden = !paste.hidden;
    $("list-paste-toggle").setAttribute("aria-expanded", String(!paste.hidden));
    $("list-paste-toggle").textContent = paste.hidden ? "Paste the rows instead" : "Hide the rows";
    if (!paste.hidden) paste.focus();
  });
  $("list-paste").addEventListener("input", () => {
    lists.csv = $("list-paste").value;
    lists.fileAsked += 1; // a file still being read no longer replaces what is typed
    if (lists.fileName) {
      lists.fileName = "";
      lists.workbook = null;
      $("list-sheet-row").hidden = true;
      $("list-file-name").textContent = "Rows pasted below";
    }
    previewListSoon();
  });
  for (const id of ["list-country", "list-instructions", "list-greeting", "list-voicemail"]) {
    $(id).addEventListener("input", previewListSoon);
  }
  $("list-form").addEventListener("submit", (event) => {
    event.preventDefault();
    createList(true);
  });
  $("list-save").addEventListener("click", () => createList(false));
  $("dnc-form").addEventListener("submit", addDoNotCall);
}

async function createList(start) {
  showError("list-error", "");
  if (!$("list-name").value.trim()) {
    showError("list-error", "Give the list a name.");
    $("list-name").focus();
    return;
  }
  if (!lists.csv.trim()) {
    showError("list-error", "Choose an Excel or CSV file of people to call, or paste the rows.");
    return;
  }
  if (lists.creating) return;
  lists.creating = true;
  $("list-start").disabled = true;
  $("list-save").disabled = true;
  try {
    const { campaign } = await api("/api/campaigns", {
      method: "POST",
      body: JSON.stringify({
        name: $("list-name").value,
        contacts_csv: lists.csv,
        country_code: $("list-country").value,
        instructions: $("list-instructions").value,
        greeting: $("list-greeting").value,
        voicemail_message: $("list-voicemail").value,
        calling_from: $("list-from").value,
        calling_until: $("list-until").value,
        timezone: $("list-timezone").value,
        at_once: Number($("list-at-once").value),
        start,
      }),
    });
    lists.byId.set(campaign.id, campaign);
    renderLists();
    resetListForm();
    const card = document.querySelector(`[data-list-id="${CSS.escape(campaign.id)}"]`);
    if (card) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    showError("list-error", error.message);
  } finally {
    lists.creating = false;
    $("list-save").disabled = false;
    showListsAvailability(); // Start calling, as far as phone calls are set up
  }
}

function resetListForm() {
  for (const id of ["list-name", "list-paste", "list-instructions", "list-greeting", "list-voicemail"]) $(id).value = "";
  $("list-file").value = "";
  lists.csv = "";
  lists.fileName = "";
  lists.workbook = null;
  lists.fileAsked += 1;
  $("list-sheet-row").hidden = true;
  $("list-file-name").textContent = "Choose an Excel or CSV file";
  $("list-drop-note").textContent = DROP_NOTE;
  $("list-drop").classList.remove("chosen");
  $("list-preview").hidden = true;
}

// ----- do not call -----

function formatNumberDigits(digits) {
  return `+${digits}`;
}

function renderDoNotCall() {
  const list = $("dnc-list");
  keepFocus(list, () => {
    list.replaceChildren();
    if (!lists.doNotCall.length) {
      const empty = document.createElement("li");
      empty.className = "dnc-empty";
      empty.textContent = "No one yet.";
      list.append(empty);
      return;
    }
    for (const entry of lists.doNotCall) {
      const item = document.createElement("li");
      const number = document.createElement("span");
      number.className = "number";
      number.textContent = formatNumberDigits(entry.number);
      const since = document.createElement("span");
      since.className = "dnc-since";
      since.textContent = entry.call_id ? `asked on a call, ${formatTime(entry.added_at)}` : `added ${formatTime(entry.added_at)}`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "link";
      remove.textContent = "Remove";
      remove.dataset.key = `dnc-${entry.number}`;
      remove.setAttribute("aria-label", `Remove ${formatNumberDigits(entry.number)} from Do not call`);
      remove.addEventListener("click", () => removeDoNotCall(entry.number));
      item.append(number, since, remove);
      list.append(item);
    }
  });
}

async function addDoNotCall(event) {
  event.preventDefault();
  showError("dnc-error", "");
  const field = $("dnc-number");
  if (!field.value.trim()) return;
  try {
    const { do_not_call } = await api("/api/do-not-call", { method: "POST", body: JSON.stringify({ number: field.value }) });
    lists.doNotCall = do_not_call;
    field.value = "";
    renderDoNotCall();
  } catch (error) {
    showError("dnc-error", error.message);
  }
}

async function removeDoNotCall(digits) {
  try {
    const { do_not_call } = await api(`/api/do-not-call/${encodeURIComponent(digits)}`, { method: "DELETE" });
    lists.doNotCall = do_not_call;
    renderDoNotCall();
  } catch (error) {
    showError("dnc-error", error.message);
  }
}

// ---------- a call's recording, and how it ended ----------

function renderDetailRecording(call) {
  const box = $("detail-recording");
  const audio = $("detail-audio");
  if (!call || !(call.recording_seconds > 0)) {
    box.hidden = true;
    if (audio.getAttribute("src")) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    return;
  }
  const source = `/api/calls/${call.id}/recording`;
  // Not set again on each update of the call: that would stop it playing.
  if (audio.getAttribute("src") !== source) {
    audio.setAttribute("src", source);
    audio.setAttribute("aria-label", `Recording of the ${callLabel(call)}`);
  }
  $("detail-recording-length").textContent = formatDuration(Math.floor(call.recording_seconds));
  $("detail-download").href = `${source}?download=1`;
  box.hidden = false;
}

function callOutcome(call) {
  if (!call) return "";
  if (call.answered_by === "voicemail") return "A voicemail answered this call.";
  if (call.answered_by === "fax") return "A fax machine answered this call.";
  if (call.transferred_to) {
    const how = call.transfer_result === "answered" ? "and they answered" : call.transfer_result ? `but ${call.transfer_result}` : "";
    return `Put through to ${call.transferred_to}${how ? `, ${how}` : ""}.`;
  }
  return "";
}

function renderDetailOutcome(call) {
  const text = callOutcome(call);
  $("detail-outcome").textContent = text;
  $("detail-outcome").hidden = !text;
}

function outcomeTags(call) {
  const tags = [];
  if (call.answered_by === "voicemail") tags.push("Voicemail");
  if (call.transferred_to) tags.push(call.transfer_result && call.transfer_result !== "answered" ? "Not put through" : "Put through");
  if (call.campaign_id) tags.push("Call list");
  if (call.recording_seconds > 0) tags.push("Recorded");
  return tags;
}


// ---------- Twilio numbers (app/numbers.py) ----------

const NUMBER_STATES = {
  connected: "Connected to Oscar",
  old_address: "Old address",
  elsewhere: "Not Oscar",
  unset: "Not set up",
  trunk: "SIP trunk",
  fax: "Fax",
};
const NUMBER_KINDS = { local: "Local", mobile: "Mobile", toll_free: "Toll-free" };

const phoneNumbers = {
  loaded: false,
  overview: null,
  confirming: null, // { sid, action: "disconnect" | "take" }: the number asking first
  busy: new Set(), // numbers with a request to Twilio on its way
  countries: [],
  kind: "local",
  results: [],
  price: null,
  buying: null, // the number asking "Buy?"
  purchasing: null, // the number being bought now: nothing else can be pressed meanwhile
};

async function loadNumbers({ keepError = false } = {}) {
  if (!(state.status && state.status.phone.enabled)) return;
  if (!keepError) showError("numbers-error", "");
  try {
    phoneNumbers.overview = await api("/api/numbers");
    phoneNumbers.loaded = true;
  } catch (error) {
    // Not asked again by itself (wrong keys would only be refused again): Refresh asks.
    phoneNumbers.loaded = true;
    showError("numbers-error", error.message);
    if (phoneNumbers.overview) renderNumbers();
    else $("numbers-list").replaceChildren();
    return;
  }
  renderNumbers();
}

function renderNumbers() {
  const overview = phoneNumbers.overview;
  if (!overview) return;
  $("numbers-trial").hidden = !overview.trial;
  $("phone-number").textContent = overview.calls_from || "no number yet";
  $("phone-number-source").textContent = !overview.calls_from
    ? " (choose one below)"
    : overview.calls_from_chosen
      ? " (chosen here)"
      : " (TWILIO_PHONE_NUMBER in .env)";
  $("numbers-more").hidden = !overview.more;
  const atStart = overview.at_start || [];
  $("numbers-at-start").hidden = !atStart.length;
  $("numbers-at-start-list").replaceChildren(
    ...atStart.map((sentence) => {
      const item = document.createElement("li");
      item.textContent = sentence;
      return item;
    }),
  );
  const list = $("numbers-list");
  keepFocus(list, () => {
    list.replaceChildren();
    if (!overview.numbers.length && !overview.caller_ids.length) {
      const empty = document.createElement("li");
      empty.className = "numbers-empty hint";
      empty.textContent = "This Twilio account has no numbers yet. Get one below.";
      list.append(empty);
      return;
    }
    for (const number of overview.numbers) list.append(numberRow(number, overview));
    for (const verified of overview.caller_ids) list.append(callerIdRow(verified, overview));
  });
}

function callsFromControl(phone, overview, canCall) {
  if (overview.calls_from === phone) {
    const chip = document.createElement("span");
    chip.className = "calls-from-chip";
    chip.textContent = "Oscar calls from this";
    return chip;
  }
  if (!canCall) return null;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "link";
  button.textContent = "Call from this";
  button.dataset.key = `from-${phone}`;
  button.setAttribute("aria-label", `Make Oscar's calls come from ${phone}`);
  button.disabled = phoneNumbers.busy.has(phone);
  button.addEventListener("click", () => chooseCallsFrom(phone));
  return button;
}

function numberRow(number, overview) {
  const item = document.createElement("li");
  item.className = `number-row state-${number.state}`;
  const top = document.createElement("div");
  top.className = "number-top";
  const phone = document.createElement("span");
  phone.className = "number-phone";
  phone.textContent = number.phone_number;
  const takesCalls = number.capabilities.voice;
  const chip = document.createElement("span");
  chip.className = `number-state state-${takesCalls ? number.state : "unset"}`;
  chip.textContent = takesCalls ? NUMBER_STATES[number.state] || number.state : "No calls";
  top.append(phone, chip);
  item.append(top);

  const detail = document.createElement("p");
  detail.className = "number-detail";
  const name = number.friendly_name && number.friendly_name !== number.phone_number ? `${number.friendly_name}. ` : "";
  detail.textContent = `${name}${takesCalls ? number.detail : "Twilio sells this number for texts only, so it cannot take or make calls."}`;
  item.append(detail);

  const actions = document.createElement("div");
  actions.className = "number-actions";
  const busy = phoneNumbers.busy.has(number.sid);
  if (["unset", "elsewhere", "old_address"].includes(number.state) && number.capabilities.voice) {
    const connect = document.createElement("button");
    connect.type = "button";
    connect.className = "secondary small";
    connect.textContent = busy ? "Connecting…" : number.state === "old_address" ? "Connect again" : "Connect to Oscar";
    connect.disabled = busy;
    connect.dataset.key = `connect-${number.sid}`;
    connect.setAttribute("aria-label", `Connect ${number.phone_number} to Oscar`);
    connect.addEventListener("click", () => {
      if (number.state === "elsewhere") askAbout(number.sid, "take");
      else connectNumber(number.sid);
    });
    actions.append(connect);
  }
  if (number.state === "connected" || number.state === "old_address") {
    const disconnect = document.createElement("button");
    disconnect.type = "button";
    disconnect.className = "link";
    disconnect.textContent = "Disconnect";
    disconnect.disabled = busy;
    disconnect.dataset.key = `disconnect-${number.sid}`;
    disconnect.setAttribute("aria-label", `Disconnect ${number.phone_number} from Oscar`);
    disconnect.addEventListener("click", () => askAbout(number.sid, "disconnect"));
    actions.append(disconnect);
  }
  const from = callsFromControl(number.phone_number, overview, number.capabilities.voice);
  if (from) actions.append(from);
  if (actions.children.length) item.append(actions);

  const asking = phoneNumbers.confirming;
  if (asking && asking.sid === number.sid) {
    const taking = asking.action === "take";
    item.append(
      confirmBox({
        id: `number-ask-${number.sid}`,
        text: taking
          ? `Take ${number.phone_number} over? ${number.detail} Its calls will reach Oscar instead, and Disconnect gives them back.`
          : `Stop Oscar answering ${number.phone_number}? Twilio is set back to how the number answered calls before.`,
        yesLabel: taking ? "Connect to Oscar" : "Disconnect",
        yesClass: taking ? "primary" : "danger-button",
        yesKey: `${asking.action}-yes-${number.sid}`,
        noLabel: "Keep it as it is",
        noKey: `${asking.action}-no-${number.sid}`,
        onYes: () => (taking ? connectNumber(number.sid) : disconnectNumber(number.sid)),
        busy: phoneNumbers.busy.has(number.sid), // one press, one request
        onNo: () => {
          phoneNumbers.confirming = null;
          renderNumbers();
          focusKey(`${taking ? "connect" : "disconnect"}-${number.sid}`);
        },
      }),
    );
  }
  return item;
}

function focusKey(key) {
  const element = document.querySelector(`[data-key="${CSS.escape(key)}"]`);
  if (element) element.focus();
}

function askAbout(sid, action) {
  phoneNumbers.confirming = { sid, action };
  renderNumbers();
  focusKey(`${action}-yes-${sid}`);
}

// A question asked in place before a change that matters: named by its text for
// screen readers, and Escape answers no.
function confirmBox({ id, text, yesLabel, yesClass, yesKey, noLabel, noKey, onYes, onNo, busy = false, className = "detail-confirm" }) {
  const box = document.createElement("div");
  box.className = className;
  box.setAttribute("role", "alertdialog");
  box.setAttribute("aria-labelledby", `${id}-text`);
  const words = document.createElement("p");
  words.id = `${id}-text`;
  words.textContent = text;
  const row = document.createElement("div");
  row.className = "actions";
  const yes = document.createElement("button");
  yes.type = "button";
  yes.className = yesClass;
  yes.dataset.key = yesKey;
  if (yesClass === "primary") {
    const label = document.createElement("span");
    label.className = "btn-label";
    label.textContent = yesLabel;
    yes.append(label);
  } else {
    yes.textContent = yesLabel;
  }
  yes.disabled = busy;
  yes.addEventListener("click", onYes);
  const no = document.createElement("button");
  no.type = "button";
  no.className = "link";
  no.textContent = noLabel;
  no.dataset.key = noKey;
  no.disabled = busy;
  no.addEventListener("click", onNo);
  box.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !busy) {
      event.stopPropagation();
      onNo();
    }
  });
  row.append(yes, no);
  box.append(words, row);
  return box;
}

function callerIdRow(verified, overview) {
  const item = document.createElement("li");
  item.className = "number-row verified";
  const top = document.createElement("div");
  top.className = "number-top";
  const phone = document.createElement("span");
  phone.className = "number-phone";
  phone.textContent = verified.phone_number;
  const chip = document.createElement("span");
  chip.className = "number-state state-verified";
  chip.textContent = "Verified caller ID";
  top.append(phone, chip);
  const detail = document.createElement("p");
  detail.className = "number-detail";
  const name = verified.friendly_name && verified.friendly_name !== verified.phone_number ? `${verified.friendly_name}. ` : "";
  detail.textContent = `${name}Verified in Twilio: Oscar can call from it, but calls to it do not reach Oscar.`;
  item.append(top, detail);
  const from = callsFromControl(verified.phone_number, overview, true);
  if (from) {
    const actions = document.createElement("div");
    actions.className = "number-actions";
    actions.append(from);
    item.append(actions);
  }
  return item;
}

async function numberAction(key, request) {
  phoneNumbers.busy.add(key);
  showError("numbers-error", "");
  renderNumbers();
  let problem = "";
  try {
    await request();
  } catch (error) {
    problem = error.message;
  } finally {
    phoneNumbers.busy.delete(key);
    renderNumbers(); // never left showing "Connecting…"
  }
  // Read back from Twilio, so the list shows what is really set; a refusal stays on screen.
  await loadNumbers({ keepError: Boolean(problem) });
  if (problem) showError("numbers-error", problem);
  loadStatus().catch(() => {});
}

function connectNumber(sid) {
  if (phoneNumbers.confirming && phoneNumbers.confirming.sid === sid) phoneNumbers.confirming = null;
  return numberAction(sid, () => api(`/api/numbers/${sid}/connect`, { method: "POST" }));
}

function disconnectNumber(sid) {
  if (phoneNumbers.confirming && phoneNumbers.confirming.sid === sid) phoneNumbers.confirming = null;
  return numberAction(sid, () => api(`/api/numbers/${sid}/disconnect`, { method: "POST" }));
}

function chooseCallsFrom(phone) {
  return numberAction(phone, () =>
    api("/api/numbers/calls-from", { method: "PUT", body: JSON.stringify({ phone_number: phone }) }),
  );
}

// ----- getting a new number -----

async function openBuyPanel() {
  const panel = $("buy-panel");
  panel.hidden = false;
  $("numbers-get").setAttribute("aria-expanded", "true");
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  if (phoneNumbers.countries.length) return;
  try {
    const { countries } = await api("/api/numbers/countries");
    phoneNumbers.countries = countries;
    fillBuyCountries();
  } catch (error) {
    $("buy-country").replaceChildren(new Option("Could not load countries", ""));
    showError("buy-error", error.message);
  }
}

function closeBuyPanel() {
  $("buy-panel").hidden = true;
  $("numbers-get").setAttribute("aria-expanded", "false");
  $("numbers-get").focus();
}

// The country to offer first: the one of the number Oscar calls from, then the browser's.
function likelyCountry() {
  const codes = new Set(phoneNumbers.countries.map((country) => country.code));
  const fromNumber = (phoneNumbers.overview && phoneNumbers.overview.calls_from) || "";
  const prefixes = { "+1": "US", "+44": "GB", "+61": "AU", "+64": "NZ", "+65": "SG", "+91": "IN", "+353": "IE", "+27": "ZA" };
  for (const [prefix, code] of Object.entries(prefixes).sort((a, b) => b[0].length - a[0].length)) {
    if (fromNumber.startsWith(prefix) && codes.has(code)) return code;
  }
  const region = (navigator.language || "").split("-")[1];
  if (region && codes.has(region.toUpperCase())) return region.toUpperCase();
  return codes.has("US") ? "US" : (phoneNumbers.countries[0] || {}).code || "";
}

function fillBuyCountries() {
  const select = $("buy-country");
  select.replaceChildren(...phoneNumbers.countries.map((country) => new Option(country.name, country.code)));
  select.value = likelyCountry();
  showBuyKinds();
}

function showBuyKinds() {
  const country = phoneNumbers.countries.find((each) => each.code === $("buy-country").value);
  const kinds = country ? country.types : [];
  if (!kinds.includes(phoneNumbers.kind)) phoneNumbers.kind = kinds[0] || "local";
  const group = $("buy-kinds");
  group.replaceChildren(
    ...kinds.map((kind) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.textContent = NUMBER_KINDS[kind] || kind;
      chip.setAttribute("aria-pressed", String(kind === phoneNumbers.kind));
      chip.addEventListener("click", () => {
        phoneNumbers.kind = kind;
        showBuyKinds();
      });
      return chip;
    }),
  );
  // Twilio searches by area code only in the US and Canada.
  const areaCodes = ["US", "CA"].includes($("buy-country").value) && phoneNumbers.kind === "local";
  $("buy-area-field").hidden = !areaCodes;
  if (!areaCodes) $("buy-area").value = "";
}

async function searchNumbers(event) {
  event.preventDefault();
  showError("buy-error", "");
  const country = $("buy-country").value;
  if (!country) {
    showError("buy-error", "Choose a country.");
    return;
  }
  const button = $("buy-search");
  button.disabled = true;
  $("buy-summary").textContent = "Asking Twilio…";
  phoneNumbers.buying = null;
  try {
    const params = new URLSearchParams({
      country,
      kind: phoneNumbers.kind,
      area_code: $("buy-area").value.trim(),
      contains: $("buy-contains").value.trim(),
    });
    const found = await api(`/api/numbers/search?${params}`);
    phoneNumbers.results = found.numbers;
    phoneNumbers.price = found.price;
    const name = $("buy-country").selectedOptions[0].textContent;
    const kind = (NUMBER_KINDS[phoneNumbers.kind] || "").toLowerCase();
    const price = found.price ? ` Each costs ${formatPrice(found.price)} a month.` : " Twilio did not say what they cost: check its pricing page before buying.";
    $("buy-summary").textContent = found.numbers.length
      ? `${found.numbers.length} ${kind} number${found.numbers.length === 1 ? "" : "s"} in ${name} that take calls.${price}`
      : `Twilio has no ${kind} numbers in ${name} like that right now. Try other digits, or another kind.`;
  } catch (error) {
    phoneNumbers.results = [];
    $("buy-summary").textContent = "";
    showError("buy-error", error.message);
  } finally {
    button.disabled = false;
  }
  renderBuyResults();
}

function formatPrice(price) {
  const amount = Number(price.monthly);
  const shown = Number.isFinite(amount) ? amount.toFixed(2) : price.monthly;
  return price.unit === "USD" ? `$${shown}` : `${shown} ${price.unit}`;
}

function renderBuyResults() {
  const list = $("buy-results");
  keepFocus(list, () => {
    list.replaceChildren();
    for (const number of phoneNumbers.results) {
      const item = document.createElement("li");
      item.className = "buy-result";
      const phone = document.createElement("p");
      phone.className = "number-phone";
      phone.textContent = number.phone_number;
      const place = document.createElement("p");
      place.className = "number-detail";
      const can = ["Calls", number.capabilities.sms ? "texts" : "", number.capabilities.mms ? "picture messages" : ""].filter(Boolean);
      place.textContent = `${number.place || "Anywhere in the country"} · ${can.join(", ")}`;
      item.append(phone, place);
      if (number.address_needed || number.beta) {
        const note = document.createElement("p");
        note.className = "buy-note";
        note.textContent = [number.address_needed ? `${number.address_needed}.` : "", number.beta ? "Twilio lists it as beta." : ""]
          .filter(Boolean)
          .join(" ");
        item.append(note);
      }
      const row = document.createElement("div");
      row.className = "buy-row";
      const price = document.createElement("span");
      price.className = "buy-price";
      price.textContent = phoneNumbers.price ? `${formatPrice(phoneNumbers.price)}/month` : "Price not given";
      const buy = document.createElement("button");
      buy.type = "button";
      buy.className = "secondary small";
      buy.textContent = "Buy";
      buy.dataset.key = `buy-${number.phone_number}`;
      buy.setAttribute("aria-label", `Buy ${number.phone_number}`);
      buy.disabled = Boolean(phoneNumbers.purchasing);
      buy.addEventListener("click", () => {
        phoneNumbers.buying = number.phone_number;
        renderBuyResults();
        focusKey(`buy-yes-${number.phone_number}`);
      });
      row.append(price, buy);
      item.append(row);
      if (phoneNumbers.buying === number.phone_number) item.append(buyConfirm(number));
      list.append(item);
    }
  });
}

function buyConfirm(number) {
  const cost = phoneNumbers.price
    ? `Twilio charges your account ${formatPrice(phoneNumbers.price)} now, and every month until you release it in Twilio's console.`
    : "Twilio did not say what it costs: it charges your account now, and every month until you release it in Twilio's console.";
  const purchasing = phoneNumbers.purchasing === number.phone_number;
  return confirmBox({
    id: `buy-ask-${number.phone_number.replace(/\D/g, "")}`,
    className: "buy-confirm",
    text: purchasing
      ? `Buying ${number.phone_number} from Twilio…`
      : `Buy ${number.phone_number}? ${cost} Calls to it reach Oscar straight away.`,
    yesLabel: purchasing ? "Buying…" : "Buy number",
    yesClass: "primary",
    yesKey: `buy-yes-${number.phone_number}`,
    noLabel: "Cancel",
    noKey: `buy-no-${number.phone_number}`,
    busy: Boolean(phoneNumbers.purchasing),
    onYes: () => buyNumber(number.phone_number),
    onNo: () => {
      phoneNumbers.buying = null;
      renderBuyResults();
      focusKey(`buy-${number.phone_number}`);
    },
  });
}

async function buyNumber(phone) {
  if (phoneNumbers.purchasing) return; // one purchase at a time
  phoneNumbers.purchasing = phone;
  $("buy-search").disabled = true;
  showError("buy-error", "");
  renderBuyResults();
  try {
    await api("/api/numbers/buy", { method: "POST", body: JSON.stringify({ phone_number: phone }) });
    phoneNumbers.results = phoneNumbers.results.filter((number) => number.phone_number !== phone);
    phoneNumbers.buying = null;
    $("buy-summary").textContent = `Bought ${phone}. It is connected to Oscar: call it to try.`;
  } catch (error) {
    showError("buy-error", error.message);
  } finally {
    phoneNumbers.purchasing = null;
    $("buy-search").disabled = false;
    renderBuyResults();
  }
  await loadNumbers();
  loadStatus().catch(() => {});
}

function setUpNumbers() {
  $("numbers-refresh").addEventListener("click", loadNumbers);
  $("numbers-get").addEventListener("click", () => ($("buy-panel").hidden ? openBuyPanel() : closeBuyPanel()));
  $("buy-close").addEventListener("click", closeBuyPanel);
  $("buy-country").addEventListener("change", showBuyKinds);
  $("buy-form").addEventListener("submit", searchNumbers);
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
  $("agent-try").addEventListener("click", tryUnsavedSettings);
  $("detail-backdrop").addEventListener("click", () => closeDetail());
  $("detail-callback").addEventListener("click", callBack);
  $("detail-copy").addEventListener("click", copyNumber);
  $("detail-link").addEventListener("click", copyCallLink);
  $("detail-block").addEventListener("click", blockCaller);
  $("blocked-form").addEventListener("submit", saveBlocked);
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
  setUpListForm();
  setUpNumbers();
  setUpSignOut();
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
    await Promise.all([
      loadAgent(),
      loadCalls(),
      loadWhatsApp(),
      loadSpending(),
      loadNotify(),
      loadBlocked(),
      loadLists(),
    ]);
    // A link to one call (?call=…) opens it.
    const linked = new URL(location.href).searchParams.get("call");
    if (linked) openDetail(linked);
  } catch (error) {
    if (goingToSignIn) return;
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
