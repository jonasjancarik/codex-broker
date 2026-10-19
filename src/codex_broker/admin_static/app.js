"use strict";

(() => {
  const KEY_STORAGE = "codexBrokerOperatorKey";
  const view = document.getElementById("view");
  const tabs = document.getElementById("tabs");
  const signIn = document.getElementById("sign-in");
  const signInForm = document.getElementById("sign-in-form");
  const signInError = document.getElementById("sign-in-error");
  const sessionActions = document.getElementById("session-actions");
  const versionLabel = document.getElementById("version");

  const TURN_STATUS = {
    starting: ["Starting", "warn"],
    queued: ["Waiting to start", "warn"],
    running: ["Running", "warn"],
    completed: ["Finished", "good"],
    failed: ["Failed", "bad"],
    timed_out: ["Timed out", "bad"],
    interrupted: ["Stopped", ""],
  };
  const AUTH_STATUS = {
    authenticated: ["Signed in", "good"],
    missing: ["Not signed in", "warn"],
    failed: ["Sign-in failing", "bad"],
    refresh_failed: ["Sign-in expired", "bad"],
    unknown: ["Not checked yet", ""],
  };
  const PROCESS_STATUS = {
    running: ["Running", "good"],
    closed: ["Shut down", ""],
    exited: ["Crashed", "bad"],
  };
  const THREAD_STATUS = {
    active: ["Open", "good"],
    archived: ["Archived", ""],
  };

  let state = { tab: "overview", ownerHash: null, filters: {}, cursors: [] };
  let refreshTimer = null;

  // ---------- helpers ----------

  function getKey() {
    try {
      return sessionStorage.getItem(KEY_STORAGE);
    } catch {
      return null;
    }
  }

  function setKey(value) {
    try {
      if (value) sessionStorage.setItem(KEY_STORAGE, value);
      else sessionStorage.removeItem(KEY_STORAGE);
    } catch {
      /* storage unavailable; key lives only in memory for this load */
    }
    memoryKey = value;
  }

  let memoryKey = getKey();

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (name === "class") el.className = value;
      else if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
      else el.setAttribute(name, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : String(child));
    }
    return el;
  }

  class Unauthorized extends Error {}

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        Authorization: `Bearer ${memoryKey}`,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
      },
      cache: "no-store",
    });
    if (response.status === 401) throw new Unauthorized();
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `The broker returned ${response.status}.`);
    return body;
  }

  function pill(map, value) {
    const [label, tone] = map[value] || [value ? String(value).replace(/_/g, " ") : "—", ""];
    return h("span", { class: `pill ${tone}`, title: value || "" }, label);
  }

  function hash(value, onOpen) {
    if (!value) return "—";
    const short = `${value.slice(0, 10)}…`;
    const label = onOpen
      ? h("button", { type: "button", class: "link mono", title: value, onclick: () => onOpen(value) }, short)
      : h("span", { title: value }, short);
    const copy = h(
      "button",
      {
        type: "button",
        class: "copy",
        title: "Copy full value",
        "aria-label": "Copy full value",
        onclick: async (event) => {
          event.stopPropagation();
          try {
            await navigator.clipboard.writeText(value);
            copy.textContent = "copied";
            setTimeout(() => (copy.textContent = "copy"), 1200);
          } catch {
            window.prompt("Copy this value:", value);
          }
        },
      },
      "copy",
    );
    return h("span", { class: "hash" }, label, copy);
  }

  function when(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    let relative;
    if (seconds < 60) relative = "just now";
    else if (seconds < 3600) relative = `${Math.floor(seconds / 60)} min ago`;
    else if (seconds < 86400) relative = `${Math.floor(seconds / 3600)} h ago`;
    else relative = `${Math.floor(seconds / 86400)} d ago`;
    return h("span", { title: date.toLocaleString() }, relative);
  }

  function duration(seconds) {
    const s = Math.floor(seconds);
    const d = Math.floor(s / 86400);
    const hrs = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d) return `${d} d ${hrs} h`;
    if (hrs) return `${hrs} h ${m} min`;
    return `${m} min`;
  }

  function table(columns, rows, emptyText) {
    if (!rows.length) return h("div", { class: "table-wrap" }, h("div", { class: "empty" }, emptyText));
    return h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, columns.map((col) => h("th", {}, col.label)))),
        h(
          "tbody",
          {},
          rows.map((row) => h("tr", {}, columns.map((col) => h("td", { class: col.cls || "nowrap" }, col.render(row))))),
        ),
      ),
    );
  }

  function stat(label, value, hint) {
    return h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), hint ? h("div", { class: "hint" }, hint) : null);
  }

  function sandboxLabel(status) {
    if (status === "healthy") return "Command sandbox is working.";
    if (status === "unsupported") return "Command sandbox is not available on this platform.";
    if (!status) return "Command sandbox has not been checked yet.";
    return `Command sandbox is not working (${status.replace(/_/g, " ")}).`;
  }

  function sum(obj) {
    return Object.values(obj || {}).reduce((a, b) => a + b, 0);
  }

  function statusSummary(byStatus) {
    const entries = Object.entries(byStatus || {});
    if (!entries.length) return h("span", { class: "muted" }, "none");
    return h("span", {}, entries.map(([status, count]) => h("span", {}, pill(TURN_STATUS, status), ` ${count} `)));
  }

  function turnMessage(turn) {
    if (!turn.publicMessage && !turn.adminMessage && !turn.errorCode) return h("span", { class: "muted" }, "—");
    return h(
      "div",
      { class: "message" },
      turn.errorCode ? h("span", { class: "label" }, `Error: ${turn.errorCode}`) : null,
      turn.adminMessage || turn.publicMessage || "",
    );
  }

  function pager(next, onPrev, onNext) {
    return h(
      "div",
      { class: "pager" },
      h("button", { type: "button", class: "ghost", disabled: !state.cursors.length, onclick: onPrev }, "Newer"),
      h("button", { type: "button", class: "ghost", disabled: !next, onclick: onNext }, "Older"),
    );
  }

  function paged(load) {
    return {
      prev: () => {
        state.cursors.pop();
        render();
      },
      next: (cursor) => {
        state.cursors.push(cursor);
        render();
      },
      cursor: () => state.cursors[state.cursors.length - 1] || "",
      load,
    };
  }

  function query(params) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) if (value) search.set(key, value);
    const text = search.toString();
    return text ? `?${text}` : "";
  }

  // ---------- views ----------

  const turnColumns = (withOwner) => [
    { label: "Started", render: (t) => when(t.createdAt) },
    { label: "Status", render: (t) => pill(TURN_STATUS, t.status) },
    ...(withOwner ? [{ label: "Owner", render: (t) => hash(t.ownerHash, openOwner) }] : []),
    { label: "Turn", render: (t) => h("span", { class: "mono", title: `Thread ${t.threadId}` }, t.turnId) },
    { label: "App", render: (t) => t.hostApp || h("span", { class: "muted" }, "—") },
    { label: "What happened", cls: "wrap", render: turnMessage },
  ];

  async function renderOverview() {
    const data = await api("/admin/api/overview");
    versionLabel.textContent = `v${data.version}`;
    const ready = data.readiness.status === "ready";
    const sandbox = data.readiness.sandboxPreflight || {};
    const sandboxOk = sandbox.status === "healthy";
    const live = data.live;
    const failedToday = (data.turnsLast24hByStatus.failed || 0) + (data.turnsLast24hByStatus.timed_out || 0);
    const turnsToday = sum(data.turnsLast24hByStatus);
    const capacity = data.maxActiveTurns ? ` of ${data.maxActiveTurns} allowed` : "no limit set";

    const banner = h(
      "div",
      { class: `banner ${ready ? (sandboxOk ? "good" : "warn") : "bad"}` },
      h("span", { class: "dot" }),
      h(
        "div",
        {},
        h("strong", {}, ready ? "The broker is ready to take requests" : "The broker is not ready to take requests"),
        h(
          "span",
          { class: "muted" },
          `Running for ${duration(data.uptimeSeconds)}. ${sandboxLabel(sandbox.status)}`,
        ),
        data.readiness.errors.length ? h("ul", {}, data.readiness.errors.map((e) => h("li", {}, e))) : null,
      ),
    );

    return [
      h("h1", {}, "Overview"),
      h("p", { class: "updated" }, `Updated ${new Date().toLocaleTimeString()} · refreshes every 10 seconds`),
      banner,
      h(
        "div",
        { class: "stats" },
        stat("Turns running now", live.activeTurns, capacity),
        stat("Turns waiting to start", live.queuedTurns),
        stat("App Servers running", live.activeAppServerChildren, data.maxPooledAppServers ? `pool limit ${data.maxPooledAppServers}` : null),
        stat("App Server restarts", live.appServerRestarts, "since the broker started"),
        stat("Turns in the last 24 h", turnsToday, failedToday ? `${failedToday} failed or timed out` : "none failed"),
        stat("Waiting for an answer", data.pendingInteractions, "approvals and questions"),
        stat("Owners active today", data.ownersActiveLast24h, `${data.ownersActiveLast7d} this week · ${data.ownersTotal} total`),
        stat("Sign-in attempts", live.authStarts, `${live.authSuccesses} succeeded · ${live.authFailures} failed`),
      ),
      h("h2", {}, "Turns in the last 24 hours"),
      h("div", { class: "card" }, statusSummary(data.turnsLast24hByStatus)),
      h("h2", {}, "Recent failures"),
      table(turnColumns(true), data.recentFailedTurns, "No turns have failed in the last 24 hours."),
    ];
  }

  function lookupForm() {
    const input = h("input", { type: "password", class: "lookup", placeholder: "Owner ID from the host app", "aria-label": "Owner ID", autocomplete: "off" });
    const result = h("span", { class: "muted", role: "status" });
    const form = h(
      "form",
      {
        class: "toolbar",
        onsubmit: async (event) => {
          event.preventDefault();
          const ownerId = input.value.trim();
          if (!ownerId) return;
          result.textContent = "Looking up…";
          try {
            const found = await api("/admin/api/owners/lookup", { method: "POST", body: JSON.stringify({ ownerId }) });
            input.value = "";
            if (found.known) openOwner(found.ownerHash);
            else result.textContent = "The broker has no threads for this owner.";
          } catch (error) {
            handleError(error, result);
          }
        },
      },
      h("label", { for: "owner-lookup" }, "Find an owner by ID"),
      input,
      h("button", { type: "submit" }, "Find"),
      result,
    );
    input.id = "owner-lookup";
    return h(
      "div",
      {},
      form,
      h("p", { class: "muted" }, "The broker stores only a fingerprint of each owner ID, so owners are listed by fingerprint. The ID you enter is not saved."),
    );
  }

  async function renderOwners() {
    const pages = paged();
    const data = await api(`/admin/api/owners${query({ limit: 50, cursor: pages.cursor() })}`);
    return [
      h("h1", {}, "Owners"),
      lookupForm(),
      table(
        [
          { label: "Owner", render: (o) => hash(o.ownerHash, openOwner) },
          { label: "Last active", render: (o) => when(o.lastActivity) },
          { label: "First seen", render: (o) => when(o.firstSeen) },
          { label: "Threads", render: (o) => o.threadCount },
          { label: "Turns in the last 24 h", cls: "wrap", render: (o) => statusSummary(o.turnsLast24hByStatus) },
          {
            label: "Latest error",
            render: (o) => (o.lastError ? h("span", {}, pill({}, o.lastError.errorCode), " ", when(o.lastError.createdAt)) : h("span", { class: "muted" }, "—")),
          },
        ],
        data.items,
        "No owners have used the broker yet.",
      ),
      pager(data.nextCursor, pages.prev, () => pages.next(data.nextCursor)),
    ];
  }

  async function renderOwnerDetail() {
    const data = await api(`/admin/api/owners/${encodeURIComponent(state.ownerHash)}`);
    const owner = data.owner;
    return [
      h("p", {}, h("button", { type: "button", class: "link", onclick: () => switchTab("owners") }, "← All owners")),
      h("h1", {}, "Owner"),
      h(
        "div",
        { class: "card" },
        h(
          "dl",
          { class: "kv" },
          h("dt", {}, "Fingerprint"),
          h("dd", {}, hash(owner.ownerHash)),
          h("dt", {}, "Last active"),
          h("dd", {}, when(owner.lastActivity)),
          h("dt", {}, "First seen"),
          h("dd", {}, when(owner.firstSeen)),
          h("dt", {}, "Turns in the last 24 h"),
          h("dd", {}, statusSummary(owner.turnsLast24hByStatus)),
          h("dt", {}, "Credentials used"),
          h("dd", {}, owner.authPrincipalHashes.map((p) => h("div", {}, hash(p)))),
        ),
      ),
      h("h2", {}, "Codex sign-in"),
      table(
        [
          { label: "Status", render: (p) => pill(AUTH_STATUS, p.authStatus) },
          { label: "Profile", render: (p) => p.profile },
          { label: "Method", render: (p) => (p.authType === "chatgpt" ? "ChatGPT account" : p.authType === "apikey" ? "API key" : p.authType || "—") },
          { label: "Credentials", render: (p) => hash(p.authPrincipalHash) },
          { label: "Last changed", render: (p) => when(p.updatedAt) },
        ],
        data.authProfiles,
        "This owner has not signed in to Codex through the broker.",
      ),
      h("h2", {}, "Threads"),
      table(
        [
          { label: "Thread", render: (t) => h("span", { class: "mono" }, t.threadId) },
          { label: "Status", render: (t) => pill(THREAD_STATUS, t.status) },
          { label: "App", render: (t) => t.hostApp || "—" },
          { label: "Bundle", render: (t) => t.bundleId || "—" },
          { label: "Settings", render: (t) => `${t.profile} / ${t.configProfile}` },
          { label: "Last updated", render: (t) => when(t.updatedAt) },
        ],
        data.threads,
        "No threads.",
      ),
      h("h2", {}, "Recent turns"),
      table(turnColumns(false), data.recentTurns, "No turns yet."),
    ];
  }

  function filterSelect(name, label, options) {
    const select = h(
      "select",
      {
        "aria-label": label,
        onchange: () => {
          state.filters[name] = select.value;
          state.cursors = [];
          render();
        },
      },
      options.map(([value, text]) => h("option", { value }, text)),
    );
    select.value = state.filters[name] || "";
    return select;
  }

  function filterInput(name, placeholder) {
    const input = h("input", { placeholder, "aria-label": placeholder, value: state.filters[name] || "" });
    input.addEventListener("change", () => {
      state.filters[name] = input.value.trim();
      state.cursors = [];
      render();
    });
    return input;
  }

  async function renderTurns() {
    const pages = paged();
    const f = state.filters;
    const data = await api(`/admin/api/turns${query({ limit: 50, cursor: pages.cursor(), status: f.status, ownerHash: f.ownerHash, errorCode: f.errorCode })}`);
    return [
      h("h1", {}, "Turns"),
      h(
        "div",
        { class: "toolbar" },
        filterSelect("status", "Status", [["", "All statuses"], ...Object.entries(TURN_STATUS).map(([k, [label]]) => [k, label])]),
        filterInput("ownerHash", "Owner fingerprint"),
        filterInput("errorCode", "Error code"),
      ),
      table(turnColumns(true), data.items, "No turns match these filters."),
      pager(data.nextCursor, pages.prev, () => pages.next(data.nextCursor)),
    ];
  }

  async function renderProcesses() {
    const pages = paged();
    const data = await api(`/admin/api/processes${query({ limit: 50, cursor: pages.cursor(), status: state.filters.processStatus })}`);
    return [
      h("h1", {}, "App Servers"),
      h("p", { class: "muted" }, "Each App Server is a Codex process the broker starts for one set of credentials and settings."),
      h(
        "div",
        { class: "stats" },
        stat("Running now", data.live.activeAppServerChildren),
        stat("Starting", data.live.pendingAppServerCreations),
        stat("Restarts", data.live.appServerRestarts, "since the broker started"),
      ),
      h("div", { class: "toolbar" }, filterSelect("processStatus", "Status", [["", "All"], ...Object.entries(PROCESS_STATUS).map(([k, [label]]) => [k, label])])),
      table(
        [
          { label: "Started", render: (p) => when(p.startedAt) },
          { label: "Status", render: (p) => pill(PROCESS_STATUS, p.status) },
          { label: "Exit code", render: (p) => (p.exitCode === null || p.exitCode === undefined ? "—" : h("span", { class: p.exitCode ? "pill bad" : "" }, p.exitCode)) },
          { label: "Process ID", render: (p) => p.pid ?? "—" },
          { label: "Credentials", render: (p) => hash(p.authPrincipalHash) },
          { label: "Settings", render: (p) => `${p.profile} / ${p.configProfile}` },
          { label: "Last seen", render: (p) => when(p.lastSeenAt) },
          { label: "Stopped", render: (p) => when(p.closedAt) },
        ],
        data.items,
        "No App Servers have started yet.",
      ),
      pager(data.nextCursor, pages.prev, () => pages.next(data.nextCursor)),
    ];
  }

  async function renderAudit() {
    const pages = paged();
    const f = state.filters;
    const data = await api(`/admin/api/audit${query({ limit: 50, cursor: pages.cursor(), action: f.action, ownerHash: f.auditOwnerHash })}`);
    if (data.actionCounts) state.actionCounts = data.actionCounts;
    const actions = Object.keys(state.actionCounts || {}).sort();
    return [
      h("h1", {}, "Audit log"),
      h(
        "div",
        { class: "toolbar" },
        filterSelect("action", "Action", [["", "All actions"], ...actions.map((a) => [a, `${a} (${state.actionCounts[a]})`])]),
        filterInput("auditOwnerHash", "Owner fingerprint"),
      ),
      table(
        [
          { label: "When", render: (e) => when(e.createdAt) },
          { label: "Action", render: (e) => h("span", { class: "mono" }, e.action) },
          { label: "Owner", render: (e) => hash(e.ownerHash, openOwner) },
          {
            label: "Thread and turn",
            render: (e) =>
              e.threadId || e.turnId
                ? h("div", { class: "mono stack" }, e.threadId ? h("div", {}, e.threadId) : null, e.turnId ? h("div", { class: "muted" }, e.turnId) : null)
                : h("span", { class: "muted" }, "—"),
          },
          {
            label: "Details",
            cls: "wrap",
            render: (e) =>
              Object.keys(e.payload || {}).length
                ? h("details", { class: "payload" }, h("summary", {}, "Show"), h("pre", {}, JSON.stringify(e.payload, null, 2)))
                : h("span", { class: "muted" }, "—"),
          },
        ],
        data.items,
        "No audit entries match these filters.",
      ),
      pager(data.nextCursor, pages.prev, () => pages.next(data.nextCursor)),
    ];
  }

  const RENDERERS = {
    overview: renderOverview,
    owners: renderOwners,
    owner: renderOwnerDetail,
    turns: renderTurns,
    processes: renderProcesses,
    audit: renderAudit,
  };

  // ---------- navigation ----------

  function openOwner(ownerHash) {
    state = { ...state, tab: "owner", ownerHash, cursors: [] };
    history.replaceState(null, "", `#owner/${ownerHash}`);
    render();
  }

  function switchTab(tab) {
    state = { ...state, tab, ownerHash: null, cursors: [] };
    history.replaceState(null, "", `#${tab}`);
    render();
  }

  function handleError(error, target) {
    if (error instanceof Unauthorized) {
      showSignIn("That operator key was not accepted. Check it and try again.");
      return;
    }
    const message = error instanceof TypeError ? "Could not reach the broker. Check that it is running." : error.message;
    if (target) target.textContent = message;
    else view.replaceChildren(h("div", { class: "error-box", role: "alert" }, message));
  }

  let renderSeq = 0;

  async function render({ quiet = false } = {}) {
    clearTimeout(refreshTimer);
    const seq = ++renderSeq;
    const activeTab = state.tab === "owner" ? "owners" : state.tab;
    for (const button of tabs.querySelectorAll("button")) {
      if (button.dataset.tab === activeTab) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
    if (!quiet) view.replaceChildren(h("p", { class: "muted" }, "Loading…"));
    try {
      const content = await RENDERERS[state.tab]();
      if (seq !== renderSeq) return;
      view.replaceChildren(...content);
    } catch (error) {
      if (seq !== renderSeq) return;
      handleError(error);
    }
    if (state.tab === "overview" && memoryKey) {
      refreshTimer = setTimeout(() => {
        if (!document.hidden) render({ quiet: true });
        else refreshTimer = setTimeout(() => render({ quiet: true }), 10000);
      }, 10000);
    }
  }

  function showSignIn(message) {
    clearTimeout(refreshTimer);
    setKey(null);
    view.replaceChildren();
    tabs.hidden = true;
    sessionActions.hidden = true;
    signIn.hidden = false;
    signInError.hidden = !message;
    signInError.textContent = message || "";
    document.getElementById("operator-key").focus();
  }

  function showApp() {
    signIn.hidden = true;
    tabs.hidden = false;
    sessionActions.hidden = false;
    const hashValue = location.hash.slice(1);
    if (hashValue.startsWith("owner/")) {
      state.tab = "owner";
      state.ownerHash = hashValue.slice("owner/".length);
    } else if (RENDERERS[hashValue] && hashValue !== "owner") {
      state.tab = hashValue;
    }
    render();
  }

  signInForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = document.getElementById("operator-key");
    const value = input.value.trim();
    if (!value) return;
    input.value = "";
    setKey(value);
    showApp();
  });

  tabs.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (button) switchTab(button.dataset.tab);
  });

  window.addEventListener("hashchange", () => {
    if (memoryKey) showApp();
  });
  document.getElementById("refresh").addEventListener("click", () => render());
  document.getElementById("sign-out").addEventListener("click", () => showSignIn());

  if (memoryKey) showApp();
  else showSignIn();
})();
