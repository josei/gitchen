const PALETTE = ["#ffb4a2", "#a8dadc", "#7b6cf6", "#ffd166", "#ff8fab", "#90caf9", "#b8e0a8", "#f6c6ea"];

const app = document.getElementById("app");
const daemonPill = document.getElementById("daemon-pill");
const nextPill = document.getElementById("next-pill");
const schedulesPill = document.getElementById("schedules-pill");

let overview = null;
let currentIssue = null;
let refreshTimer = null;
let filter = { state: null, label: null, folder: null };
let filtered = null;

const esc = (s) => String(s ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const roleOf = (who) => {
  const role = String(who ?? "").split(" (")[0].trim();
  return role === "daemon" ? "system" : role;   // the daemon is the system acting on a role's behalf
};
const cleanText = (text) => String(text ?? "").replace(/\n\s*NEXT:.*\s*$/i, "").trim();

// Human-readable age; the exact ISO timestamp stays available on hover.
function relTime(iso) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(then).toLocaleDateString();
}
const hash = (s) => { let h = 0; for (const ch of String(s)) h = (h * 31 + ch.codePointAt(0)) >>> 0; return h; };
const colorFor = (role) => PALETTE[hash(role) % PALETTE.length];

const HATS = ["toque", "cap", "beanie", "bandana", "crown"];
const TOOLS = ["whisk", "spoon", "ladle", "pin", "cleaver", "fork"];

function hatSvg(kind, color) {
  switch (kind) {
    case "toque":
      return `<path d="M18 16c-3-6 2-12 7-13 1-4 9-5 11-1 4-2 9 1 8 6 4 2 3 7-1 8z" fill="#fff" stroke="#3b3150" stroke-width="1.5"/>
              <rect x="17" y="14" width="30" height="6" rx="3" fill="#fff" stroke="#3b3150" stroke-width="1.5"/>`;
    case "cap":
      return `<path d="M14 16c0-8 8-13 18-13s18 5 18 13z" fill="${color}" stroke="#3b3150" stroke-width="1.5"/>
              <path d="M12 16h34c4 0 5 3 2 5H15c-3 0-4-2-3-5z" fill="${color}" stroke="#3b3150" stroke-width="1.5"/>`;
    case "beanie":
      return `<path d="M15 16c0-8 7-13 17-13s17 5 17 13z" fill="${color}" stroke="#3b3150" stroke-width="1.5"/>
              <rect x="14" y="13" width="36" height="7" rx="3" fill="${color}" stroke="#3b3150" stroke-width="1.5"/>
              <circle cx="32" cy="3" r="4" fill="#fff" stroke="#3b3150" stroke-width="1.5"/>`;
    case "bandana":
      return `<path d="M13 16c5-7 12-10 19-10s14 3 19 10l-4 3c-4-5-9-7-15-7s-11 2-15 7z" fill="${color}" stroke="#3b3150" stroke-width="1.5"/>
              <circle cx="50" cy="17" r="3" fill="${color}" stroke="#3b3150" stroke-width="1.5"/>`;
    default: // crown
      return `<path d="M15 16l3-12 6 6 8-9 8 9 6-6 3 12z" fill="#ffd166" stroke="#3b3150" stroke-width="1.5"/>
              <rect x="15" y="14" width="34" height="4" rx="1.5" fill="#ffd166" stroke="#3b3150" stroke-width="1.5"/>`;
  }
}

function toolSvg(kind) {
  const steel = "#b9b2c9";
  const wrap = (inner, angle = 16) => `<g transform="translate(50 47) rotate(${angle})"><g class="tool">${inner}</g></g>`;
  switch (kind) {
    case "whisk":
      return wrap(`<rect x="-1.4" y="-9" width="2.8" height="11" rx="1.4" fill="${steel}"/>
                   <path d="M-5 1q5 10 10 0M-3.4 1q3.4 8 6.8 0" fill="none" stroke="${steel}" stroke-width="1.5"/>`);
    case "spoon":
      return wrap(`<rect x="-1.2" y="-9" width="2.4" height="12" rx="1.2" fill="${steel}"/>
                   <ellipse cx="0" cy="5" rx="4" ry="5" fill="${steel}"/>`);
    case "ladle":
      return wrap(`<rect x="-1.2" y="-9" width="2.4" height="12" rx="1.2" fill="${steel}"/>
                   <circle cx="0" cy="6" r="4.5" fill="none" stroke="${steel}" stroke-width="2"/>`, 24);
    case "pin":
      return wrap(`<rect x="-2.2" y="-8" width="4.4" height="18" rx="2.2" fill="#d9a066"/>
                   <rect x="-1.1" y="-11" width="2.2" height="4" rx="1" fill="#b57b45"/>
                   <rect x="-1.1" y="9" width="2.2" height="4" rx="1" fill="#b57b45"/>`, 35);
    case "cleaver":
      return wrap(`<rect x="-1.2" y="-9" width="2.4" height="10" rx="1.2" fill="#8a7f9b"/>
                   <rect x="-6" y="0" width="12" height="9" rx="1.5" fill="${steel}" stroke="#8a7f9b" stroke-width="1"/>`, 10);
    default: // fork
      return wrap(`<rect x="-1.2" y="-8" width="2.4" height="13" rx="1.2" fill="${steel}"/>
                   <path d="M-4 -13v5M0 -13v5M4 -13v5" stroke="${steel}" stroke-width="1.6" stroke-linecap="round"/>`);
  }
}

function roleIndex(role) {
  const list = overview?.roles || [];
  const index = list.indexOf(role);
  return index >= 0 ? index : hash(role);
}

function chibi(role, { active = false } = {}) {
  const color = colorFor(role);
  const index = roleIndex(role);
  const hat = HATS[index % HATS.length];
  const tool = TOOLS[index % TOOLS.length];
  return `
  <svg viewBox="0 0 64 72" role="img" aria-label="${esc(role)}" class="${active ? "active" : ""}">
    <ellipse cx="32" cy="67.5" rx="15" ry="3.5" fill="#3b3150" opacity=".14"/>
    <rect x="20" y="43" width="24" height="21" rx="9" fill="${color}" stroke="#3b3150" stroke-width="1.6"/>
    <rect x="26" y="47" width="12" height="10" rx="2.5" fill="#fff" stroke="#3b3150" stroke-width="1.2"/>
    <path d="M29 51.5h6" stroke="${color}" stroke-width="1.8" stroke-linecap="round"/>
    <circle cx="32" cy="27" r="18" fill="#fff0e4" stroke="#3b3150" stroke-width="1.8"/>
    <ellipse cx="25.5" cy="27" rx="2.5" ry="3" fill="#3b3150"/>
    <ellipse cx="38.5" cy="27" rx="2.5" ry="3" fill="#3b3150"/>
    <circle cx="26.3" cy="25.8" r="1" fill="#fff"/>
    <circle cx="39.3" cy="25.8" r="1" fill="#fff"/>
    ${active
      ? `<path d="M28.5 33.2q3.5 5.6 7 0z" fill="#ff7a9c" stroke="#3b3150" stroke-width="1.4" stroke-linejoin="round"/>`
      : `<path d="M28.5 33.2q3.5 3.2 7 0" fill="none" stroke="#3b3150" stroke-width="1.6" stroke-linecap="round"/>`}
    <ellipse cx="20" cy="32" rx="3.6" ry="2.4" fill="#ff9fbd" opacity=".7"/>
    <ellipse cx="44" cy="32" rx="3.6" ry="2.4" fill="#ff9fbd" opacity=".7"/>
    ${hatSvg(hat, color)}
    ${toolSvg(tool)}
  </svg>`;
}

async function getJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${url}: ${response.status}`);
  return response.json();
}

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 3000);
}

/* --- markdown, enough of it ------------------------------------------------ */

function md(text) {
  const fences = [];
  // Markdown treats a single newline as a soft break: it joins the lines into
  // one paragraph. Two newlines are the paragraph boundary (the split below).
  const softWrap = (block) => block.trim().replace(/\s*\n\s*/g, " ");
  let out = esc(text)
    .replace(/```([\s\S]*?)```/g, (_, code) => {
      fences.push(code.trim());
      return `\u0000fence${fences.length - 1}\u0000`;
    })
    .replace(/^#### (.+)$/gm, "<h4>$1</h4>")
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h3>$1</h3>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img src="$2" alt="$1" loading="lazy">')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" rel="noreferrer" target="_blank">$1</a>');
  out = out.replace(/^(?:- (.+))$/gm, "<li>$1</li>");
  out = out.replace(/(?:^|\n)((?:<li>.*<\/li>\n?)+)/g, (_, list) => `<ul>${list}</ul>`);
  return out
    .split(/\n{2,}/)
    .map((block) => {
      if (block.includes("\u0000fence")) {
        return block.split(/(\u0000fence\d+\u0000)/).map((part) => {
          const fence = part.match(/^\u0000fence(\d+)\u0000$/);
          if (fence) return `<pre>${fences[Number(fence[1])]}</pre>`;
          return part.trim() ? `<p>${softWrap(part)}</p>` : "";
        }).join("");
      }
      const table = renderTable(block.trim());
      if (table) return table;
      return /^\s*<(h3|h4|ul|pre)/.test(block) ? block : `<p>${softWrap(block)}</p>`;
    })
    .join("");
}

function renderTable(block) {
  const lines = block.split("\n").map((line) => line.trim()).filter((line) => line.startsWith("|"));
  if (lines.length < 2) return null;
  const cells = (line) => line.replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  const separator = cells(lines[1]);
  if (!separator.every((cell) => /^:?-{2,}:?$/.test(cell))) return null;
  const head = cells(lines[0]);
  const rows = lines.slice(2).map(cells);
  const th = head.map((cell) => `<th>${cell}</th>`).join("");
  const tr = rows.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("");
  return `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div>`;
}

/* --- header ---------------------------------------------------------------- */

function renderHeader() {
  const projectName = document.getElementById("project-name");
  if (projectName && overview?.project) {
    projectName.innerHTML = `<em>${esc(overview.project)}</em>'s gitchen`;
    document.title = `${overview.project}'s gitchen`;
  }
  const daemon = overview?.daemon || {};
  daemonPill.textContent = daemon.running ? `kitchen open · ${daemon.pid}` : "kitchen closed";
  daemonPill.className = `pill ${daemon.running ? "ok" : "off"}`;

  const schedules = overview?.schedules || [];
  schedulesPill.textContent = `⏰ ${schedules.length} ${schedules.length === 1 ? "schedule" : "schedules"}`;
  schedulesPill.classList.toggle("hidden", !schedules.length);

  nextPill.textContent = budgetLabel();
  nextPill.className = `pill ${budgetWarning() ? "off" : ""}`;
  nextPill.classList.toggle("hidden", !(overview?.budget || overview?.paused));
}

function budgetLabel() {
  if (overview?.paused) return `paused after a failed turn · ${String(overview.paused.reason).slice(0, 80)}`;
  const b = overview?.budget;
  if (!b) return "";
  const window = b.per ? ` · per ${b.per}` : " · until reset";
  if (b.over) return `budget reached · $${b.spent.toFixed(2)} of $${b.limit.toFixed(2)} · run gitchen budget reset`;
  return `$${b.spent.toFixed(2)} of $${b.limit.toFixed(2)}${window}`;
}

function budgetWarning() {
  return Boolean(overview?.paused || overview?.budget?.over);
}

function money(usd) {
  return `$${Number(usd || 0).toFixed(2)}`;
}

/* --- overview --------------------------------------------------------------- */

// Every issue with a live turn, mapped to the role cooking it. With parallel
// turns there can be several; the first is only the one the pot shows.
function cookingMap() {
  return new Map((overview?.holders || []).map((h) => [h.issue, h.role]));
}

function issueRow(issue, cooking = cookingMap()) {
  const cookRole = cooking.get(issue.id);
  // Show the scope on every row once the project has more than one folder;
  // hiding one of them made the list look inconsistent.
  const showScope = (overview?.folders || []).length > 1 && issue.folder;
  return `<a class="issue-row ${cookRole ? "cooking" : ""}" href="#/issues/${issue.id}">
    <span class="who-dot" style="background:${colorFor(issue.assignee || "")}" title="${esc(issue.assignee || "unassigned")}"></span>
    <span class="title"><span class="num">#${issue.id}</span>${showScope
      ? ` <span class="scope">${esc(issue.folder)}</span>` : ""} ${esc(issue.title)}</span>
    ${cookRole
      ? `<span class="chip cooking">cooking · ${esc(cookRole)}</span>`
      : `<span class="chip ${esc(issue.state)}">${esc(issue.state)}</span>`}
    ${issue.cost_usd > 0 ? `<span class="chip cost" title="what its turns cost">${money(issue.cost_usd)}</span>` : ""}
  </a>`;
}

/* --- the kitchen scene ------------------------------------------------------- */

const STOVE_SLOTS = 4;

function scene(activeIssue, activeRole) {
  const roles = overview?.roles || [];
  const titles = new Map([...(overview.queue || []), ...(overview.blocked || [])].map((i) => [i.id, i.title]));
  const live = (overview.holders || []).map((h) => ({ ...h, title: h.title || titles.get(h.issue) || "" }));
  const busy = new Set(live.map((h) => h.role));
  // Every busy role walks to the stove and stands beside the others there, in
  // the order its turn started (slot 0 is next to the pot). Past the slots the
  // stove has room for, a busy cook works in place in the line instead.
  const stove = [...new Set([...live.map((h) => h.role), activeRole].filter(Boolean))].slice(0, STOVE_SLOTS);
  const cooks = roles.map((role, index) => `
    <button type="button" class="cook ${stove.includes(role) ? "active" : busy.has(role) ? "busy" : ""}"
            style="--i:${index}${stove.includes(role) ? `;--slot:${stove.indexOf(role)}` : ""}"
            data-cook="${esc(role)}" title="${esc(role)} — what it loads"
            aria-label="${esc(role)}: read the context it loads" aria-haspopup="dialog">
      ${chibi(role, { active: busy.has(role) || stove.includes(role) })}
      <span class="cook-name">${esc(role)}</span>
    </button>`).join("");
  // Many roles: shrink the cooks (and hide their names, kept as tooltips)
  // so the line wraps into a few rows instead of scrolling sideways.
  const crowd = roles.length > 14 ? "crowd-s" : roles.length > 8 ? "crowd-m" : "";
  const others = Math.max(0, live.length - 1);
  // One turn: its issue is the headline. Several: list every one, so nothing
  // cooking is hidden behind the first.
  const headline = live.length > 1
    ? `<ul class="stove-list">${live.map((h) => `<li>
        <span class="who-dot" style="background:${colorFor(h.role || "")}"></span>
        <a href="#/issues/${h.issue}"><span class="num">#${h.issue}</span> ${esc(h.title || "")}</a>
        <span class="stove-role">${esc(h.role || "")}</span></li>`).join("")}</ul>`
    : activeIssue
      ? `<a class="scene-issue" href="#/issues/${activeIssue.id}">#${activeIssue.id} · ${esc(activeIssue.title)}</a>`
      : `<span class="scene-issue">${overview.queue.length ? `${overview.queue.length} waiting` : "nothing waiting"}</span>`;
  const who = live.length > 1 ? `${live.length} on the stove` : `on the stove · ${esc(activeRole)}`;
  return `<section class="card scene">
    <div class="scene-head">
      <h2><span class="ico">${activeIssue ? "🍳" : "💤"}</span>${activeIssue ? who : "the stove is cold"}</h2>
      ${headline}
    </div>
    <div class="kitchen ${activeIssue ? "cooking" : ""} ${crowd}">
      <div class="awning"></div>
      <div class="window"><span class="sun"></span><span class="cloud"></span></div>
      <div class="shelf"><span class="jar j1"><b></b></span><span class="jar j2"><b></b></span><span class="jar j3"><b></b></span></div>
      <div class="rail"><i></i><i></i><i></i></div>
      <div class="stove">
        ${activeIssue ? `<span class="flame"><i></i><i></i><i></i></span>` : ""}
        <span class="pot">${activeIssue ? `<a class="pot-order" href="#/issues/${activeIssue.id}" title="${others ? `and ${others} more on the stove` : ""}">#${activeIssue.id}${others ? `<small>+${others}</small>` : ""}</a>` : ""}</span>
      </div>
      <span class="steam s1"></span><span class="steam s2"></span><span class="steam s3"></span>
      <div class="floor">${cooks || `<p class="quiet">no cooks on the line yet ✧</p>`}</div>
    </div>
  </section>`;
}

function costNote(turns) {
  if (!turns?.length) return "";
  return `<div class="bubble-cost">${turns.map((turn) => {
    const tokens = Number(turn.input || 0) + Number(turn.output || 0) + Number(turn.cache_read || 0);
    const parts = [esc(turn.model || "unknown model"), money(turn.usd)];
    if (tokens) parts.push(`${tokens.toLocaleString()} tokens`);
    if (turn.seconds) parts.push(`${turn.seconds}s`);
    return parts.join(" · ");
  }).join("<br>")}</div>`;
}

function bubble(entry) {
  const role = roleOf(entry.who);
  return `<div class="bubble-row">
    <div class="bubble-head">
      ${chibi(role)}
      <span class="meta"><a href="#/issues/${entry.issue}">#${entry.issue}</a> · ${esc(role)} · <span title="${esc(entry.when)}">${esc(relTime(entry.when))}</span></span>
    </div>
    <div class="bubble">
      <div class="text">${md(cleanText(entry.text))}${entry.truncated ? `… <a class="more" href="#/issues/${entry.issue}">read the full comment →</a>` : ""}</div>
    </div>
    ${costNote(entry.turns)}
  </div>`;
}

// The framework keeps `done` (accepted, nothing to merge) and `merged`
// (accepted, commits fast-forwarded) apart; the view groups them under done.
const STATE_GROUPS = { done: ["done", "merged"] };
const statesOf = (state) => STATE_GROUPS[state] || [state];

// `updated` is an ISO time, so it compares as text; an issue without one sorts last.
const FINISHED = new Set(["done", "canceled"]);
const byLatest = (a, b) => (b.updated || "").localeCompare(a.updated || "") || b.id - a.id;

const STATE_ICONS = { open: "🥣", blocked: "🧊", done: "🍰", canceled: "💨" };

function renderOverview() {
  const active = overview.active_issue?.id ?? null;
  const activeIssue = overview.active_issue
    || overview.queue.find((i) => i.id === active)
    || overview.blocked.find((i) => i.id === active)
    || null;
  const activeRole = overview.holders?.[0]?.role ?? activeIssue?.assignee ?? null;
  const counts = overview.counts || {};
  const labels = overview.labels || [];
  const folders = overview.folders || [];

  let list;
  if (filtered !== null) {
    const heading = [
      filter.state || "every order",
      filter.folder ? `scope: ${esc(filter.folder)}` : "",
      filter.label ? `#${esc(filter.label)}` : "",
    ].filter(Boolean).join(" · ");
    list = `<div class="card">
      <h2 class="filter-head"><span class="ico">🔎</span><span class="filter-title">${heading} <span class="count">${filtered.length}</span></span><button class="clear-filter" data-clear>clear</button></h2>
      ${filtered.length ? filtered.map((i) => issueRow(i)).join("") : `<p class="quiet">nothing here ✧</p>`}
    </div>`;
  } else {
    const queue = overview.queue.length
      ? overview.queue.map((i) => issueRow(i)).join("")
      : `<p class="quiet">the queue is empty. time for tea ♡</p>`;
    const blocked = overview.blocked.length
      ? overview.blocked.map((i) => issueRow(i)).join("")
      : `<p class="quiet">nothing blocked ✧</p>`;
    list = `<div class="grid">
      <section><div class="card"><h2><span class="ico">🍽️</span>queue <span class="count">${overview.queue.length}</span></h2>${queue}</div></section>
      <section><div class="card"><h2><span class="ico">🧊</span>blocked <span class="count">${overview.blocked.length}</span></h2>${blocked}</div></section>
    </div>`;
  }

  ensureSkeleton();
  setRegion("scene", scene(activeIssue, activeRole));
  syncStats(counts);
  syncFolders(folders);
  syncLabels(labels);
  setRegion("list", list);
}

/* --- in-place rendering -------------------------------------------------------
   Replacing the whole page on every update restarted each animation and made a
   pressed button pop back up and then drop in again as a brand-new element. The
   overview is a handful of regions; a region is only touched when its markup
   changed, and the counters and labels just flip their `active` class. */

const REGIONS = ["scene", "stats", "folders", "labels", "list"];

function ensureSkeleton() {
  if (!app.querySelector("#r-scene")) {
    app.innerHTML = REGIONS.map((name) => `<div id="r-${name}"></div>`).join("");
  }
}

function setRegion(name, html) {
  const el = document.getElementById(`r-${name}`);
  if (el && el._html !== html) {
    const before = name === "scene" ? cookPlaces(el) : null;
    el.innerHTML = html;
    el._html = html;
    if (before) walkCooks(el, before);
  }
}

/* The scene is redrawn whole on every change, so a cook that changes place
   (to the stove when its turn starts, back to the line when it ends, or along
   the line as others leave it) would jump. Instead each cook starts where it
   stood and walks to its new place: record the places, redraw, then slide
   each cook from its old place to the new one (FLIP). `translate` is used,
   not `transform`, so the cooks' own sway and hop animations keep running. */
function cookPlaces(root) {
  // Relative to the kitchen, not the page: the list of turns above the kitchen
  // grows and shrinks with the turns, which moves the whole kitchen, and the
  // cooks must walk across its floor, not drift with it.
  const places = new Map();
  const kitchen = root.querySelector(".kitchen");
  if (!kitchen) return places;
  const origin = kitchen.getBoundingClientRect();
  root.querySelectorAll(".cook[data-cook]").forEach((cook) => {
    const r = cook.getBoundingClientRect();
    places.set(cook.dataset.cook, { left: r.left - origin.left, top: r.top - origin.top });
  });
  return places;
}

function walkCooks(root, before) {
  if (!before.size || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const now = cookPlaces(root);
  root.querySelectorAll(".cook[data-cook]").forEach((cook) => {
    const was = before.get(cook.dataset.cook);
    const at = now.get(cook.dataset.cook);
    if (!was || !at) return;
    const dx = was.left - at.left;
    const dy = was.top - at.top;
    if (Math.abs(dx) < 2 && Math.abs(dy) < 2) return;
    const far = Math.hypot(dx, dy);
    cook.style.transition = "none";
    cook.style.translate = `${dx}px ${dy}px`;
    cook.classList.add("walking");
    cook.getBoundingClientRect(); // apply the start place before moving
    cook.style.transition = `translate ${Math.min(2.4, 0.6 + far / 400)}s ease-in-out`;
    cook.style.translate = "0px 0px";
    const done = () => {
      cook.classList.remove("walking");
      cook.style.transition = "";
      cook.style.translate = "";
    };
    cook.addEventListener("transitionend", done, { once: true });
    setTimeout(done, 2600); // in case the transition is interrupted
  });
}

function syncStats(counts) {
  const el = document.getElementById("r-stats");
  if (!el) return;
  if (!el.firstElementChild) {
    el.innerHTML = `<div class="stats">${["open", "blocked", "done", "canceled"].map((state) =>
      `<button class="stat" data-state="${state}" data-icon="${STATE_ICONS[state]}"><b>0</b>${state}</button>`).join("")}</div>`;
  }
  el.querySelectorAll(".stat").forEach((button) => {
    const state = button.dataset.state;
    button.classList.toggle("active", filter.state === state);
    const total = String(statesOf(state).reduce((n, s) => n + ((counts || {})[s] ?? 0), 0));
    const number = button.querySelector("b");
    if (number.textContent !== total) number.textContent = total;
  });
}

function syncFolders(folders) {
  const el = document.getElementById("r-folders");
  if (!el) return;
  const key = folders.map((folder) => `${folder.name}:${folder.count}`).join("\u0000");
  if (el._key !== key) {
    el.innerHTML = folders.length > 1
      ? `<div class="labels scopes"><span class="filter-label">scope</span>${folders.map((folder) =>
          `<button class="chip scope" data-folder="${esc(folder.name)}" title="filter by ${esc(folder.name)}">${esc(folder.name)} <b>${folder.count}</b></button>`).join("")}</div>`
      : "";
    el._key = key;
  }
  el.querySelectorAll("[data-folder]").forEach((chip) => {
    chip.classList.toggle("active", filter.folder === chip.dataset.folder);
  });
}

function syncLabels(labels) {
  const el = document.getElementById("r-labels");
  if (!el) return;
  const key = labels.join("\u0000");
  if (el._key !== key) {
    el.innerHTML = labels.length
      ? `<div class="labels">${labels.map((label) =>
          `<button class="chip label" data-label="${esc(label)}">#${esc(label)}</button>`).join("")}</div>`
      : "";
    el._key = key;
  }
  el.querySelectorAll("[data-label]").forEach((chip) => {
    chip.classList.toggle("active", filter.label === chip.dataset.label);
  });
}

// A click answers instantly; the list follows when its data arrives.
function showFilterNow() {
  if (!overview) return;
  syncStats(overview.counts);
  syncFolders(overview.folders || []);
  syncLabels(overview.labels || []);
}

/* --- filters ----------------------------------------------------------------- */

async function applyFilter() {
  if (!filter.state && !filter.label && !filter.folder) {
    filtered = null;
    renderOverview();
    return;
  }
  try {
    const states = filter.state ? statesOf(filter.state) : [null];
    const lists = await Promise.all(states.map((state) => {
      const params = new URLSearchParams();
      if (state) params.set("state", state);
      if (filter.label) params.set("label", filter.label);
      if (filter.folder) params.set("folder", filter.folder);
      return getJSON(`/api/issues?${params}`);
    }));
    filtered = lists.flatMap((list) => list.issues);
    // The server orders by priority, which is the queue's order. A finished
    // issue has no place in a queue, so show the most recently moved first.
    if (FINISHED.has(filter.state)) filtered.sort(byLatest);
    renderOverview();
  } catch (error) {
    toast("could not read the orders");
  }
}

function toggleState(state) {
  filter.state = filter.state === state ? null : state;
  showFilterNow();
  applyFilter();
}

function toggleLabel(label) {
  filter.label = filter.label === label ? null : label;
  showFilterNow();
  applyFilter();
}

function toggleFolder(folder) {
  filter.folder = filter.folder === folder ? null : folder;
  showFilterNow();
  applyFilter();
}

function popHearts(x, y) {
  for (let n = 0; n < 5; n += 1) {
    const heart = document.createElement("span");
    heart.className = "pop-heart";
    heart.textContent = ["♥", "✦", "♡", "✧", "★"][n % 5];
    heart.style.cssText = `left:${x}px;top:${y}px;--dx:${(n - 2) * 18}px;--r:${(n - 2) * 16}deg;color:${PALETTE[(n * 3) % PALETTE.length]};animation-delay:${n * 60}ms`;
    document.body.appendChild(heart);
    setTimeout(() => heart.remove(), 1400);
  }
}

/* --- what a cook loads -------------------------------------------------------
   The drawing is the project's own roles, so clicking one asks the daemon's
   same prompt builder what that role receives: its system prompt, the shared
   context, the workspace rules, the live issue and history when it has one,
   and the directive it must end with. */

let rolePanel = null;
let rolePanelPrompt = null;
let rolePanelFocus = null;

function closeRolePanel() {
  if (!rolePanel) return;
  rolePanel.remove();
  rolePanel = null;
  rolePanelPrompt = null;
  document.body.classList.remove("panel-open");
  if (rolePanelFocus && typeof rolePanelFocus.focus === "function") rolePanelFocus.focus();
  rolePanelFocus = null;
}

function rolePanelHtml(data) {
  const live = data.live?.length
    ? `<span class="chip cooking">${data.live.length > 1 ? `${data.live.length} turns cooking` : "cooking now"}</span>`
    : `<span class="chip">idle</span>`;
  const chips = [
    live,
    `<span class="chip role">${data.model ? `model ${esc(data.model)}` : "machine default model"}</span>`,
    `<span class="chip base">${esc(data.harness)}</span>`,
    `<span class="chip base">${data.workspace === "none" ? "no repo" : "worktree"}</span>`,
    data.program ? `<span class="chip">program ${esc(data.program)}</span>` : "",
  ].filter(Boolean).join("");
  const issue = data.issue
    ? `<a class="role-issue" href="#/issues/${data.issue.id}" data-close-panel>#${data.issue.id} · ${esc(data.issue.title)}</a>`
    : `<span class="role-issue quiet">no issue assigned right now</span>`;
  const tools = (data.tools || []).length
    ? data.tools.map((tool) => `<code>${esc(tool)}</code>`).join(" ")
    : `<span class="quiet">none</span>`;
  const outcomes = Object.entries(data.outcomes || {});
  // The prompt is ordered for caching (shared, workspace, posting rules, then
  // the role and issue). The panel reads better with the role prompt first,
  // so it reorders the parts it shows; the raw prompt keeps the real order.
  const raw = data.sections || [];
  const roleAt = raw.findIndex((section) => section.title === null);
  const directiveAt = raw.map((section) => section.title).lastIndexOf(null);
  const roleSection = roleAt >= 0 ? raw[roleAt] : null;
  const directiveSection = directiveAt >= 0 && directiveAt !== roleAt ? raw[directiveAt] : null;
  const middle = raw.filter((_, index) => index !== roleAt && index !== directiveAt);
  const pick = (title) => middle.filter((section) => section.title === title);
  const issueSections = middle.filter((section) => (section.title || "").startsWith("Issue #"));
  const known = new Set(["Where you may edit", "Roles you can hand work to",
                         "Assignment history", "Posting your report"]);
  const other = middle.filter((section) => !known.has(section.title)
    && !(section.title || "").startsWith("Issue #"));
  const ordered = [
    roleSection,
    ...pick("Where you may edit"),
    ...pick("Roles you can hand work to"),
    ...issueSections,
    ...pick("Assignment history"),
    ...other,
  ].filter(Boolean);
  const sections = ordered.map((section) => {
    const isRole = section === roleSection;
    const title = isRole ? "Role prompt" : section.title;
    const note = isRole
      ? `<p class="role-note">agents/${esc(data.role)}.md · frontmatter stripped</p>`
      : "";
    return `<section class="role-section"><h3>${esc(title)}</h3>${note}<div class="role-md">${md(section.body)}</div></section>`;
  }).join("");
  // The directive values are the only role-specific part of the ending; the
  // rule itself is hidden with the other boilerplate, so show the values as a
  // compact footer instead of another section.
  const ends = directiveSection
    ? `<div class="role-ends"><b>ends with</b> ${md(directiveSection.body)}</div>`
    : "";
  const prompt = data.prompt
    ? `<div class="role-actions"><button type="button" class="role-copy" data-copy-prompt>copy this prompt</button></div>
       <details class="role-raw"><summary>the turn prompt${data.prompt_omits_static ? " · shared context, workspace and boilerplate omitted" : ""}</summary><pre>${esc(data.prompt)}</pre></details>`
    : `<p class="role-note role-idle">No issue is assigned, so no turn is running. The sections above are the standing context every turn loads; a turn adds the issue, its body, and its assignment history.</p>`;
  return `<div class="role-head">
    <button type="button" class="role-close" data-close-panel aria-label="close">×</button>
    ${chibi(data.role, { active: Boolean(data.live?.length) })}
    <div class="role-title">
      <h2>${esc(data.role)}</h2>
      <p class="role-desc">${esc(data.description || "no description")}</p>
    </div>
  </div>
  <div class="role-body">
    <div class="role-chips">${chips}</div>
    <div class="role-issue-row">${issue}</div>
    <div class="role-meta">
      <div><b>MCP tools</b>${tools}</div>
      ${outcomes.length ? `<div><b>outcomes</b>${outcomes.map(([name, meaning]) =>
        `<span class="chip role-outcome"><b>${esc(name)}</b> ${esc(meaning)}</span>`).join(" ")}</div>` : ""}
    </div>
    <div class="role-sections">${sections}</div>
    ${ends}
    ${prompt}
  </div>`;
}

async function openRolePanel(role, trigger = null) {
  closeRolePanel();
  rolePanelFocus = trigger || document.activeElement;
  const overlay = document.createElement("div");
  overlay.className = "role-panel";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", `${role}: what it loads`);
  overlay.innerHTML = `<div class="role-sheet"><div class="role-head">
    <button type="button" class="role-close" data-close-panel aria-label="close">×</button>
    <div class="role-title"><h2>${esc(role)}</h2><p class="role-desc">reading its context…</p></div>
  </div></div>`;
  document.body.append(overlay);
  document.body.classList.add("panel-open");
  rolePanel = overlay;
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay || event.target.closest("[data-close-panel]")) return void closeRolePanel();
    if (!event.target.closest("[data-copy-prompt]") || !rolePanelPrompt) return;
    if (!navigator.clipboard) return void toast("copy is not available here");
    navigator.clipboard.writeText(rolePanelPrompt).then(
      () => toast("full prompt copied"),
      () => toast("could not copy the prompt"));
  });
  overlay.querySelector(".role-close")?.focus();
  try {
    const data = await getJSON(`/api/roles/${encodeURIComponent(role)}`);
    if (rolePanel !== overlay) return;
    rolePanelPrompt = data.prompt || null;
    overlay.innerHTML = `<div class="role-sheet">${rolePanelHtml(data)}</div>`;
    overlay.querySelector(".role-close")?.focus();
  } catch (error) {
    if (rolePanel !== overlay) return;
    overlay.innerHTML = `<div class="role-sheet"><div class="role-head">
      <button type="button" class="role-close" data-close-panel aria-label="close">×</button>
      <div class="role-title"><h2>${esc(role)}</h2>
      <p class="role-desc">could not read this role's context.</p></div>
    </div></div>`;
  }
}

/* --- schedules ---------------------------------------------------------------
   A schedule is not an order and its body can be long, so it lives behind one
   header button instead of taking a card above the queue. The panel shows the
   full text, not a truncated preview. */

let schedulesPanel = null;
let schedulesPanelFocus = null;

function schedulePanelHtml(schedules) {
  const body = schedules.length
    ? schedules.map((s) => `
      <div class="schedule">
        <div class="schedule-top">
          <span class="schedule-name">${esc(s.name)}</span>
          <code>${esc(s.cron)}</code>
          <span class="chip role" title="assignee"><i class="dot" style="background:${colorFor(s.assignee)}"></i>${esc(s.assignee)}</span>
          <span class="chip prio" title="priority">p${esc(s.priority)}</span>
          ${(s.labels || []).map((label) => `<span class="chip label">#${esc(label)}</span>`).join("")}
        </div>
        <div class="schedule-title">${esc(s.title)}</div>
        ${s.body ? `<div class="schedule-full role-md">${md(s.body)}</div>` : ""}
        <div class="schedule-meta">${s.last_fired
          ? `last fired ${esc(relTime(s.last_fired.replace(" ", "T")))}` : "not fired yet"}</div>
      </div>`).join("")
    : `<p class="quiet">no schedules yet ✧</p>`;
  return `<div class="role-head">
    <button type="button" class="role-close" data-close-schedules aria-label="close">×</button>
    <div class="role-title">
      <h2>⏰ schedules</h2>
      <p class="role-desc">${schedules.length} scheduled ${schedules.length === 1 ? "job" : "jobs"}</p>
    </div>
  </div>
  <div class="role-body"><div class="role-sections">${body}</div></div>`;
}

function closeSchedulesPanel() {
  if (!schedulesPanel) return;
  schedulesPanel.remove();
  schedulesPanel = null;
  document.body.classList.remove("panel-open");
  if (schedulesPanelFocus && typeof schedulesPanelFocus.focus === "function") schedulesPanelFocus.focus();
  schedulesPanelFocus = null;
}

function openSchedulesPanel(trigger = null) {
  closeSchedulesPanel();
  closeRolePanel();
  schedulesPanelFocus = trigger || document.activeElement;
  const overlay = document.createElement("div");
  overlay.className = "role-panel";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", "schedules");
  overlay.innerHTML = `<div class="role-sheet">${schedulePanelHtml(overview?.schedules || [])}</div>`;
  document.body.append(overlay);
  document.body.classList.add("panel-open");
  schedulesPanel = overlay;
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay || event.target.closest("[data-close-schedules]")) closeSchedulesPanel();
  });
  overlay.querySelector(".role-close")?.focus();
}

schedulesPill.addEventListener("click", () => openSchedulesPanel(schedulesPill));

// Pictures in comments and bodies open full size over the page; click or Esc closes.
let lightbox = null;
function closeLightbox() {
  if (lightbox) { lightbox.remove(); lightbox = null; }
}
document.addEventListener("click", (event) => {
  if (lightbox) { closeLightbox(); return; }
  const img = event.target.closest && event.target.closest(".body img, .bubble img");
  if (!img) return;
  lightbox = document.createElement("div");
  lightbox.className = "lightbox";
  lightbox.setAttribute("role", "dialog");
  lightbox.setAttribute("aria-label", img.alt || "picture");
  const full = document.createElement("img");
  full.src = img.currentSrc || img.src;
  full.alt = img.alt;
  const caption = document.createElement("p");
  caption.textContent = img.alt ? `${img.alt} — click anywhere to close` : "click anywhere to close";
  lightbox.append(full, caption);
  document.body.append(lightbox);
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (schedulesPanel) return void closeSchedulesPanel();
  if (rolePanel) return void closeRolePanel();
  closeLightbox();
});

app.addEventListener("click", (event) => {
  const cook = event.target.closest("[data-cook]");
  if (cook && !event.target.closest("a")) {
    popHearts(event.clientX, event.clientY);
    return void openRolePanel(cook.dataset.cook, cook);
  }
  const state = event.target.closest("[data-state]");
  if (state) return void toggleState(state.dataset.state);
  const label = event.target.closest("[data-label]");
  if (label) return void toggleLabel(label.dataset.label);
  const folder = event.target.closest("[data-folder]");
  if (folder) return void toggleFolder(folder.dataset.folder);
  if (event.target.closest("[data-clear]")) {
    filter = { state: null, label: null, folder: null };
    showFilterNow();
    applyFilter();
  }
});

/* --- issue detail ------------------------------------------------------------ */

function renderIssue() {
  const issue = currentIssue;
  const comments = (issue.comments || []).length
    ? issue.comments.map((c) => bubble({ issue: issue.id, who: c.who, when: c.when,
                                         text: c.text, turns: c.turns })).join("")
    : `<p class="quiet">no notes yet ✧</p>`;

  app.innerHTML = `
    <a class="back" href="#/">← back to the kitchen</a>
    <div class="card detail-head">
      <h1>issue #${issue.id} · ${esc(issue.title)}</h1>
      <div class="chips">
        <span class="chip state ${esc(issue.state)}" title="state"><i class="dot"></i>${esc(issue.state)}</span>
        <span class="chip prio" title="priority">p${esc(issue.priority)}</span>
        ${issue.folder ? `<span class="chip scope" title="scope">${esc(issue.folder)}</span>` : ""}
        ${["open", "blocked"].includes(issue.state)
          ? `<span class="chip role" title="assignee"><i class="dot" style="background:${colorFor(issue.assignee || "")}"></i>${esc(issue.assignee || "unassigned")}</span>`
          : ""}
        ${(issue.blocked_by || []).length ? `<span class="chip waiting" title="waiting on">waiting on ${issue.blocked_by.map((n) => `#${esc(n)}`).join(" ")}</span>` : ""}
        ${issue.cost_usd > 0 ? `<span class="chip cost" title="what its turns cost">${money(issue.cost_usd)}</span>` : ""}
        ${(issue.labels || []).map((l) => `<span class="chip label" title="label">#${esc(l)}</span>`).join("")}
        <span class="chip base" title="base branch">⎇ ${esc(issue.base_branch || "main")}</span>
      </div>
      <div class="body">${md(issue.body)}</div>
    </div>
    <div class="card comments"><h2><span class="ico">💌</span>notes <span class="count">${(issue.comments || []).length}</span></h2>${comments}</div>
    ${eventsCard(issue)}`;
}

function eventsCard(issue) {
  const events = issue.events || [];
  if (!events.length) return "";
  const rows = events.map((e) => `<li><span class="when">${esc(relTime(e.at))}</span> <b>${esc(e.kind)}</b>
    — ${esc(e.reason)}</li>`).join("");
  return `<div class="card events-card"><h2><span class="ico">📋</span>kitchen notes</h2><ul>${rows}</ul></div>`;
}

/* --- routing + live updates --------------------------------------------------- */

function route() {
  const match = location.hash.match(/^#\/issues\/(\d+)$/);
  if (match) {
    loadIssue(Number(match[1]));
  } else {
    currentIssue = null;
    if (overview) renderOverview();
  }
}

async function loadOverview() {
  overview = await getJSON("/api/overview");
  renderHeader();
  if (!currentIssue) {
    if (filtered !== null) await applyFilter();   // keep the active filter fresh
    else renderOverview();
  }
}

async function loadIssue(id) {
  currentIssue = await getJSON(`/api/issues/${id}`);
  renderIssue();
  renderHeader();
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    try {
      await loadOverview();
      if (currentIssue) await loadIssue(currentIssue.id);
    } catch (error) {
      toast("could not refresh the kitchen");
    }
  }, 250);
}

function startLive() {
  const source = new EventSource("/events");
  source.addEventListener("change", scheduleRefresh);
  source.addEventListener("error", () => { /* EventSource reconnects on its own */ });
}

window.addEventListener("hashchange", () => {
  closeSchedulesPanel();
  closeRolePanel();
  route();
});


// SSE is instant when it works, but a proxy (e.g. a Cloudflare quick tunnel)
// may buffer the stream. Polling keeps the view current without a reload.
setInterval(() => {
  if (document.visibilityState === "visible") scheduleRefresh();
}, 10000);

// `?nosse` is for screenshots and printing: an open EventSource keeps the
// page "loading" forever, which makes headless screenshot tools wait.
const live = !new URLSearchParams(location.search).has("nosse");
loadOverview().then(route).then(live ? startLive : () => {}).catch(() => {
  app.innerHTML = `<p class="loading">the kitchen is not reachable.</p>`;
});
