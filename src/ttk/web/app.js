// Time-to-Kill UI. Plain ES module, no build step. All data is inserted with
// textContent (never innerHTML), so values from odds feeds or notes cannot inject markup.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
  });
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body && body.detail;
    const message = Array.isArray(detail)
      ? detail.map((d) => `${d.loc?.slice(-1)[0] ?? ""} ${d.msg}`).join("; ")
      : detail || `The server answered ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return body;
}

// ------------------------------------------------------------------ formatting

const american = (n) => (n == null ? "–" : (Math.round(n) > 0 ? "+" : "") + Math.round(n));
const pct = (x, digits = 1) => (x == null ? "–" : `${(x * 100).toFixed(digits)}%`);
const signedPct = (x, digits = 1) =>
  x == null ? "–" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(digits)}%`;
const pts = (x) => (x == null ? "–" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)} pts`);
const money = (x) => (x == null ? "–" : `${x < 0 ? "−" : ""}$${Math.abs(x).toFixed(2)}`);
const signedMoney = (x) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}$${Math.abs(x).toFixed(2)}`);
const lineText = (x) => (x == null ? "" : x === 0 ? "PK" : `${x > 0 ? "+" : ""}${x}`);
const signClass = (x) => (x == null ? "" : x > 0 ? "pos" : x < 0 ? "neg" : "");
const eastern = (iso, opts = {}) =>
  new Date(iso).toLocaleString("en-US", {
    timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "2-digit", ...opts,
  }) + " ET";
const todayEastern = () =>
  new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date());
const easternDateOf = (iso) =>
  new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date(iso));
const ago = (minutes) => {
  if (minutes == null) return "never";
  if (minutes < 1.5) return "just now";
  if (minutes < 90) return `${Math.round(minutes)} min ago`;
  if (minutes < 60 * 36) return `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(minutes / 1440)} days ago`;
};
const untilText = (iso) => {
  if (!iso) return "";
  const hours = (new Date(iso) - Date.now()) / 3.6e6;
  if (hours < 1) return "within the hour";
  if (hours < 36) return `in ${Math.round(hours)} h`;
  return `in ${Math.round(hours / 24)} days`;
};

const SPORT_NAMES = { NFL: "NFL", CFB: "College football", NBA: "NBA", NCAAB: "College basketball" };
const sportName = (s) => SPORT_NAMES[s] ?? s;
const MARKET_NAMES = { SPREAD: "Spread", MONEYLINE: "Moneyline", TOTAL: "Total" };

/** A registry name like "ncaab-ml-spread-implied-eff 1" in plain words. */
function modelName(raw) {
  const name = String(raw).replace(/\s*\(.*\)$/, "").replace(/ [^ ]+$/, "");
  const [sport] = name.split("-");
  const s = sportName(sport.toUpperCase());
  const flavor = name.includes("anchored") ? "market-anchored"
    : name.includes("spread-implied") ? "spread-implied"
    : name.includes("standalone") || name.includes("-key-") ? "standalone" : "";
  const market = name.includes("-ml-") ? "moneyline" : name.includes("-total-") ? "total" : "spread";
  const inputs = name.endsWith("features") ? "EPA and QB"
    : name.endsWith("inseason") ? "in-season efficiency"
    : name.endsWith("lineup-prev") ? "lineups"
    : name.endsWith("injury") ? "lineups and injury report"
    : name.endsWith("eff") ? "efficiency"
    : name.endsWith("pace") ? "pace and efficiency" : "";
  return { title: `${s} ${market}`, detail: [flavor, inputs].filter(Boolean).join(", ") };
}

// ------------------------------------------------------------------ glossary

const TERMS = {
  edge: ["Edge", "How much more likely the model thinks this side is than the market does, in percentage points. +2 pts means the model says 52% where the market says 50%."],
  ev: ["Expected value", "The average profit per $1 bet if the model's probability is right, after the book's cut. Positive means the price is better than fair."],
  fair: ["Fair odds", "The price at which this bet would exactly break even if the model is right."],
  push: ["Push", "A tie with the line: the stake comes back. Whole-number lines can push; half-point lines can't."],
  uncertainty: ["Uncertainty", "How settled the model's number is. High uncertainty (thin data, early season) stops a bet from qualifying."],
  data: ["Data quality", "How complete and fresh the inputs are: odds age, how many books, the model's inputs."],
  oddsage: ["Odds age", "Minutes since the books' prices were last confirmed. Stale prices can't qualify."],
  linemove: ["Line move", "The main line when first seen, and now. A move toward a side means money came in on it."],
  clv: ["Closing-line value", "How your price compares with the final price before kickoff. Beating the close again and again is the best early sign of real skill, long before wins and losses settle it."],
  vsmarket: ["Compared with the market", "How much worse (+) or better (−) the model's probabilities were than the closing market's, game by game (paired log loss)."],
  z: ["z-score", "How many standard errors the model is from the market. Within about ±2 is noise; below −2 would mean genuinely better than the market."],
  decided: ["Decided games", "Finished games that didn't push. A model needs 30 at each timing before its score is shown."],
  timing: ["Timing", "When the prediction was recorded: 24 hours or 1 hour before kickoff."],
  kelly: ["Kelly fraction", "Kelly is the stake that grows a bankroll fastest if the edge is real. Betting a quarter of it (0.25) cuts the swings a lot for a small cost."],
  drawdown: ["Drop from the peak", "How far the bankroll is below its best point. New bets pause after the drop you set."],
  roi: ["Return", "Profit divided by the total staked."],
  QUALIFIED: ["Qualified", "Passes every check: a confident model, a real edge, positive value, fresh data, and a model that has earned trust. Only these are recommended."],
  LEAN: ["Lean", "The model favors this side, but at least one check fails. For now that is always because the model is still being tested."],
  PASS: ["Pass", "The model doesn't favor this side at this price."],
  NO_BET: ["No bet", "The data is too stale or too thin to judge this one."],
  testing: ["In testing", "A model being forward-tested: its predictions are recorded and scored, never recommended."],
  seed: ["Seed", "Fixes the random numbers so the same simulation gives the same answer. Leave it blank for a fresh run."],
  maxline: ["Worst line still worth it", "The worst number you could take at today's best price and still expect a profit."],
  joint: ["Chance all legs win", "The chance every leg hits. Legs in the same game are simulated together, because they move together."],
  winrate: ["Win rate", "Wins divided by wins plus losses (pushes left out)."],
  breakeven: ["Break-even rate", "How often bets at these prices must win just to break even."],
};

function term(key) {
  return el("button", { type: "button", class: "term", "data-term": key, "aria-label": `What is ${TERMS[key]?.[0] ?? key}?`, text: "?" });
}

const pop = $("#term-pop");
function showTerm(button) {
  const entry = TERMS[button.dataset.term];
  if (!entry) return;
  pop.replaceChildren(el("strong", { text: entry[0] }), el("span", { text: entry[1] }));
  pop.hidden = false;
  const r = button.getBoundingClientRect();
  const width = Math.min(300, window.innerWidth - 32);
  const left = Math.max(16, Math.min(r.left + window.scrollX - 12, window.scrollX + window.innerWidth - width - 16));
  pop.style.left = `${left}px`;
  pop.style.top = `${r.bottom + window.scrollY + 8}px`;
}
document.addEventListener("click", (event) => {
  const button = event.target.closest(".term");
  if (button) { event.preventDefault(); event.stopPropagation(); showTerm(button); return; }
  pop.hidden = true;
});
window.addEventListener("scroll", () => { pop.hidden = true; }, { passive: true });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") pop.hidden = true; });

function toast(message) {
  const t = $("#toast");
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 4200);
}

// ------------------------------------------------------------------ theme

const THEMES = ["system", "light", "dark"];
function currentTheme() {
  return document.documentElement.dataset.theme || "system";
}
function setTheme(theme) {
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("ttk-theme", theme); } catch { /* not remembered */ }
  $("#theme-label").textContent = `Theme: ${theme}`;
}
$("#theme-toggle").addEventListener("click", () => {
  setTheme(THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length]);
});
$("#theme-label").textContent = `Theme: ${currentTheme()}`;

// ------------------------------------------------------------------ routing

const PAGES = ["home", "today", "slip", "bets", "sim", "models"];
const loaders = {};

function route() {
  const [page, sub] = (location.hash.slice(1) || "home").split("/");
  const name = PAGES.includes(page) ? page : "home";
  for (const p of PAGES) $(`#page-${p}`).hidden = p !== name;
  for (const a of $$(".nav a")) {
    if (a.dataset.page === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  pop.hidden = true;
  loaders[name]?.(sub);
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

function go(hash) {
  if (location.hash === `#${hash}`) route();
  else location.hash = hash;
}

function pressPill(group, button) {
  for (const b of $$(".pill", group)) b.setAttribute("aria-pressed", String(b === button));
}

function empty(title, text, ...actions) {
  return el("div", { class: "empty" }, el("strong", { text: title }), el("span", { text }), ...actions);
}

function metric(label, value, sub = "", cls = "", termKey = null) {
  return el("div", { class: "metric" },
    el("div", { class: "label" }, el("span", { text: label }), termKey ? term(termKey) : null),
    el("div", { class: `value ${cls}`, text: value }),
    sub ? el("div", { class: "sub", text: sub }) : null);
}

// ------------------------------------------------------------------ shared data

let books = [];
async function loadBooks() {
  books = await api("/api/sportsbooks");
  books.sort((a, b) => Number(b.bettable) - Number(a.bettable) || a.key.localeCompare(b.key));
  const options = () => books.map((b) => el("option", { value: b.key, text: b.bettable ? b.key : `${b.key} (not one of yours)` }));
  $("#slip-book").replaceChildren(...options());
  $("#bet-book").replaceChildren(...options());
  if (slip.book) $("#slip-book").value = slip.book;
  else slip.book = $("#slip-book").value;
}

// ------------------------------------------------------------------ home

loaders.home = async () => {
  const now = new Date();
  $("#home-date").textContent = now.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });
  const hour = now.getHours();
  $("#home-title").textContent = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  try {
    const s = await api("/api/status");
    renderHome(s);
  } catch (error) {
    $("#home-lede").textContent = `Couldn't reach the app's data: ${error.message}`;
  }
};

function renderHome(s) {
  const c = s.collection;
  const fresh = c.minutes_since_poll != null && c.minutes_since_poll < 45;
  const alerts = c.alerts || [];
  $("#home-lede").textContent = alerts.length
    ? `${alerts.length === 1 ? "One thing needs" : `${alerts.length} things need`} your attention below. Nothing qualifies as a bet yet: every model is still being tested.`
    : fresh
      ? "Everything is running. Nothing qualifies as a bet yet: every model is still being tested against the market."
      : "Odds collection looks stalled. Check that the collector task is running.";

  // Today
  const today = $("#home-today");
  const soon = s.upcoming.filter((u) => u.next_24h > 0);
  const total = soon.reduce((n, u) => n + u.next_24h, 0);
  today.replaceChildren(
    el("h2", { text: "Today" }),
    el("div", {}, el("div", { class: "big-number", text: String(total) }),
      el("div", { class: "muted small", text: total === 1 ? "game in the next 24 hours" : "games in the next 24 hours" })),
    soon.length
      ? el("dl", { class: "kv" }, ...soon.flatMap((u) => [el("dt", { text: sportName(u.sport) }), el("dd", { text: String(u.next_24h) })]))
      : el("p", { class: "muted small", text: nextKickoffText(s.upcoming) }),
    el("a", { class: "link", href: "#today", text: "See today's games →" }),
  );

  // Collection
  const col = $("#home-collection");
  col.replaceChildren(
    el("h2", { text: "Data collection" }),
    el("div", { class: `status-line ${fresh ? "good" : "bad"}` }, el("span", { class: "dot" }),
      el("span", { text: fresh ? "Collecting odds" : "Odds collection has stopped" })),
    el("dl", { class: "kv" },
      el("dt", { text: "Last odds update" }), el("dd", { text: ago(c.minutes_since_poll) }),
      el("dt", { text: "PropLine requests left today" }), el("dd", { text: c.quota_remaining == null ? "–" : String(c.quota_remaining) }),
      el("dt", { text: "Last player-prop pull" }), el("dd", { text: c.last_props_pull ? ago((Date.now() - new Date(c.last_props_pull)) / 6e4) : "none yet" })),
    alerts.length ? el("ul", { class: "alert-list" }, ...alerts.map((a) => el("li", { text: a.message }))) : null,
  );

  // Bankroll
  const b = s.bankroll;
  const bank = $("#home-bankroll");
  bank.replaceChildren(
    el("h2", { text: "Your bankroll" }),
    ...(b.configured
      ? [el("div", {}, el("div", { class: "big-number", text: money(b.balance) }),
            el("div", { class: "muted small", text: `${b.open_wagers} open bet${b.open_wagers === 1 ? "" : "s"} · ${money(b.open_exposure)} riding` })),
         b.drawdown < 0 ? el("p", { class: "small neg", text: `${signedPct(b.drawdown)} from its peak` }) : null,
         el("a", { class: "link", href: "#bets/bankroll", text: "Manage bankroll →" })]
      : [el("p", { class: "muted small", text: "Not set up yet. Add a starting amount to turn on staking limits, so one bad day can't sink you." }),
         el("a", { class: "link", href: "#bets/bankroll", text: "Set up my bankroll →" })]),
  );

  // Models
  $("#home-min-decided").textContent = String(s.min_decided);
  $("#home-model-list").replaceChildren(...sortModels(s.models).map((m) => progressRow(m, s)));
}

function nextKickoffText(upcoming) {
  const next = upcoming.filter((u) => u.next_kickoff).sort((a, b) => new Date(a.next_kickoff) - new Date(b.next_kickoff))[0];
  return next ? `Next: ${sportName(next.sport)} ${untilText(next.next_kickoff)} (${eastern(next.next_kickoff)}).` : "No games on the schedule yet.";
}

function modelState(m, s) {
  const need = s.min_decided;
  if (m.snapshots === 0) {
    const u = s.upcoming.find((x) => x.sport === m.sport);
    return { text: u?.next_kickoff ? `Starts ${untilText(u.next_kickoff)}` : "Waiting for its season", cls: "" };
  }
  if (m.z == null || m.decided < need) return { text: `${m.decided} of ${need} games`, cls: "" };
  if (m.z <= -2) return { text: `Beating the market (z ${m.z.toFixed(1)})`, cls: "pos" };
  if (m.z >= 2) return { text: `Worse than the market (z +${m.z.toFixed(1)})`, cls: "neg" };
  return { text: `Level with the market (z ${m.z >= 0 ? "+" : ""}${m.z.toFixed(1)})`, cls: "" };
}

const SPORT_ORDER = ["NFL", "CFB", "NBA", "NCAAB"];
const sortModels = (models) => [...models].sort((a, b) =>
  SPORT_ORDER.indexOf(a.sport) - SPORT_ORDER.indexOf(b.sport) || b.decided - a.decided || a.model.localeCompare(b.model));

function progressRow(m, s) {
  const n = modelName(m.model);
  const state = modelState(m, s);
  const share = Math.min(1, m.decided / s.min_decided);
  return el("div", { class: "progress-row" },
    el("div", { class: "label" }, el("span", { text: n.title }), el("span", { text: n.detail })),
    el("div", { class: "bar-track", role: "img", "aria-label": `${m.decided} of ${s.min_decided} games` },
      el("div", { class: "bar-fill", style: `width:${(share * 100).toFixed(1)}%` })),
    el("div", { class: `state ${state.cls}`, text: state.text }));
}

// ------------------------------------------------------------------ today's games

let lastCard = null;
let cardSport = "";
const cardDate = $("#card-date");
cardDate.value = todayEastern();

const CHECK_NAMES = {
  probability: "Model confidence", edge: "Edge over the market", expected_value: "Value at this price",
  model_validated: "Model trusted (not in testing)", odds_fresh: "Fresh odds", odds_current: "Fresh odds",
  data_quality: "Data quality", uncertainty: "Uncertainty", model_health: "Model health",
};
const checkName = (n) => CHECK_NAMES[n] ?? n.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());

function verdictText(entry) {
  const failing = entry.checks.filter((c) => !c.passed);
  if (entry.classification === "QUALIFIED") return "Every check passes.";
  if (entry.classification === "PASS") {
    return entry.edge > 0
      ? `Close, but not at this price: the ${pts(entry.edge)} edge doesn't cover the book's cut (value ${entry.ev_percent.toFixed(1)}%).`
      : "The model doesn't favor this side.";
  }
  if (!failing.length) return TERMS[entry.classification]?.[1] ?? "";
  const names = failing.map((c) => checkName(c.name).toLowerCase());
  return `Not a bet yet: ${names.join(", ")}.`;
}

function compareBars(entry) {
  const row = (who, cls, p) => el("div", { class: `row ${cls}` },
    el("span", { class: "who", text: who }),
    el("div", { class: "track" }, el("div", { class: "fill", style: `width:${(p * 100).toFixed(1)}%` })),
    el("span", { class: "val", text: pct(p) }));
  return el("div", { class: "compare", role: "img", "aria-label": `Model ${pct(entry.model_probability)}, market ${pct(entry.market_probability)}` },
    row("Model", "model", entry.model_probability),
    row("Market", "market", entry.market_probability),
    el("div", { class: `edge ${signClass(entry.edge)}` }, `Edge ${pts(entry.edge)} `, term("edge")));
}

function detailsBlock(entry) {
  const dl = el("dl", { class: "details-grid" });
  const item = (label, value, key, cls = "") => dl.append(el("div", {},
    el("dt", {}, el("span", { text: label }), key ? term(key) : null), el("dd", { class: cls, text: value })));
  item("Expected value", `${entry.ev_percent >= 0 ? "+" : ""}${entry.ev_percent.toFixed(1)}%`, "ev", signClass(entry.ev_percent));
  item("Fair odds", american(entry.fair_american_odds), "fair");
  item("Chance of a push", pct(entry.push_probability), "push");
  item("Uncertainty", entry.uncertainty.replace("_", " ").toLowerCase(), "uncertainty");
  item("Data quality", entry.data_quality.toLowerCase(), "data");
  item("Odds age", `${Math.round(entry.odds_age_minutes)} min`, "oddsage");
  item("Line move", entry.line_opening != null && entry.line_current != null
    ? `${lineText(entry.line_opening)} → ${lineText(entry.line_current)}` : "–", "linemove");
  item("Model", modelName(entry.model_version).detail || entry.model_version, null);
  const checks = el("ul", { class: "checks" }, ...entry.checks.map((c) =>
    el("li", { class: c.passed ? "" : "fail", text: `${checkName(c.name)}: ${c.actual} (needs ${c.required})` })));
  return el("details", {}, el("summary", { text: "Details and checks" }), dl, checks);
}

function sideRow(entry) {
  const track = el("button", { type: "button", text: "Track bet" });
  track.addEventListener("click", () => openBetDialog({
    game: { id: entry.game_id, matchup: entry.matchup, commence_time: entry.commence_time, sport: entry.sport },
    market: entry.market, selection: entry.selection, line: entry.line,
    american_odds: entry.american_odds, sportsbook: entry.sportsbook,
    stake: entry.stake && entry.stake.recommended > 0 ? entry.stake.recommended : null,
  }));
  const add = el("button", { type: "button", class: "ghost", text: "Add to parlay" });
  add.addEventListener("click", () => {
    if (!slip.legs.length) setSlipBook(entry.sportsbook);
    addLeg({ game_id: entry.game_id, market: entry.market, selection: entry.selection,
             line: entry.line, label: `${entry.bet} (${entry.matchup})` });
    toast(`Added to your parlay: ${entry.bet}`);
  });
  return el("div", { class: "side" },
    el("div", { class: "pick" },
      el("div", { class: "pick-title" },
        el("span", { class: `chip ${entry.classification}`, text: entry.classification.replace("_", " ") }),
        term(entry.classification),
        el("span", { text: entry.bet })),
      el("p", { class: "price", text: `Best price ${american(entry.american_odds)} at ${entry.sportsbook}` }),
      el("p", { class: "verdict", text: verdictText(entry) })),
    compareBars(entry),
    el("div", { class: "side-actions" }, track, add),
    detailsBlock(entry));
}

function gameCard(entries) {
  const first = entries[0];
  const sim = el("button", { type: "button", class: "ghost small-btn", text: "Simulate" });
  sim.addEventListener("click", () => openSimulation(first.game_id, first.commence_time, first.sport));
  return el("article", { class: "game" },
    el("header", { class: "game-head" },
      el("span", { class: "chip sport", text: sportName(first.sport) }),
      el("h2", { text: first.matchup }),
      el("span", { class: "when", text: eastern(first.commence_time) }),
      el("span", { class: "spacer" }),
      sim),
    ...entries.map(sideRow));
}

function cardFilter() {
  const classes = $("#filter-class").value;
  const edge = $("#filter-edge").value;
  return {
    sport: cardSport || null,
    classes: classes ? classes.split(",") : null,
    minEdge: edge === "" ? null : Number(edge) / 100,
    book: $("#filter-book").value || null,
    search: $("#card-search").value.trim().toLowerCase(),
  };
}
function matches(entry, f) {
  return (!f.sport || entry.sport === f.sport)
    && (!f.classes || f.classes.includes(entry.classification))
    && (f.minEdge == null || entry.edge >= f.minEdge - 1e-12)
    && (!f.book || entry.sportsbook === f.book)
    && (!f.search || entry.matchup.toLowerCase().includes(f.search));
}

function renderGames() {
  if (!lastCard) return;
  const f = cardFilter();
  const shown = lastCard.entries.filter((e) => matches(e, f));
  const byGame = new Map();
  for (const e of shown) {
    if (!byGame.has(e.game_id)) byGame.set(e.game_id, []);
    byGame.get(e.game_id).push(e);
  }
  const games = [...byGame.values()].sort((a, b) => new Date(a[0].commence_time) - new Date(b[0].commence_time));
  const box = $("#card-games");
  if (games.length) box.replaceChildren(...games.map(gameCard));
  else if (lastCard.entries.length) {
    const clear = el("button", { type: "button", class: "ghost", text: "Clear filters" });
    clear.addEventListener("click", clearFilters);
    box.replaceChildren(empty("No games match your filters", `${lastCard.entries.length} priced sides are hidden.`, clear));
  } else {
    box.replaceChildren(empty("No priced games for this date",
      lastCard.unmodeled.length ? "Games with odds are listed below, but no model priced them." : "There are no games with a live market on this date. Try another date."));
  }
  const booksSeen = [...new Set(lastCard.entries.map((e) => e.sportsbook))].sort();
  const sel = $("#filter-book");
  const chosen = sel.value;
  sel.replaceChildren(el("option", { value: "", text: "Any book" }), ...booksSeen.map((b) => el("option", { value: b, text: b })));
  if (chosen) sel.value = booksSeen.includes(chosen) ? chosen : "";
}

function clearFilters() {
  $("#filter-class").value = "";
  $("#filter-edge").value = "";
  $("#filter-book").value = "";
  $("#card-search").value = "";
  cardSport = "";
  pressPill($("#sport-pills"), $("#sport-pills .pill"));
  saveFilters();
  renderGames();
}

function saveFilters() {
  try {
    localStorage.setItem("ttk-card-filters", JSON.stringify({
      sport: cardSport, cls: $("#filter-class").value, edge: $("#filter-edge").value, book: $("#filter-book").value,
    }));
  } catch { /* not remembered */ }
}
function restoreFilters() {
  try {
    const saved = JSON.parse(localStorage.getItem("ttk-card-filters") || "null");
    if (!saved || Array.isArray(saved)) return;
    cardSport = saved.sport || "";
    $("#filter-class").value = saved.cls || "";
    $("#filter-edge").value = saved.edge || "";
    if (saved.book) $("#filter-book").append(el("option", { value: saved.book, text: saved.book }));
    $("#filter-book").value = saved.book || "";
    const pill = $(`#sport-pills .pill[data-sport="${cardSport}"]`);
    if (pill) pressPill($("#sport-pills"), pill);
  } catch { /* ignore */ }
}

for (const pill of $$("#sport-pills .pill")) {
  pill.addEventListener("click", () => {
    cardSport = pill.dataset.sport;
    pressPill($("#sport-pills"), pill);
    saveFilters();
    renderGames();
  });
}
for (const id of ["filter-class", "filter-book"]) $(`#${id}`).addEventListener("change", () => { saveFilters(); renderGames(); });
$("#filter-edge").addEventListener("input", () => { saveFilters(); renderGames(); });
$("#card-search").addEventListener("input", renderGames);
$("#filter-clear").addEventListener("click", clearFilters);
cardDate.addEventListener("change", () => loadCard());
$("#card-refresh").addEventListener("click", () => loadCard());

let cardSeq = 0;
let loadingRetry = null;
async function loadCard({ quiet = false } = {}) {
  const seq = ++cardSeq;
  clearTimeout(loadingRetry);
  const started = Date.now();
  const banner = $("#card-banner");
  const refresh = $("#card-refresh");
  refresh.disabled = true;
  let timer = null;
  if (!quiet) {
    $("#card-summary").replaceChildren();
    $("#card-games").replaceChildren(...[1, 2, 3].map(() => el("div", { class: "skeleton-card" })));
    const note = el("div", { class: "banner info" }, el("span", { text: "Building the card…" }));
    banner.replaceChildren(note);
    timer = setInterval(() => {
      const s = Math.round((Date.now() - started) / 1000);
      note.firstChild.textContent = `Building the card… ${s} s. The first load after the app starts fits the NFL model, which takes about 20 seconds.`;
    }, 1000);
  }
  try {
    const card = await api(`/api/card?date=${encodeURIComponent(cardDate.value || todayEastern())}`);
    if (seq !== cardSeq) return;
    lastCard = card;
    renderCardSummary(card);
    banner.replaceChildren();
    if (card.loading && card.loading.length) {
      banner.replaceChildren(el("div", { class: "banner" },
        el("span", { text: `${card.loading.map(sportName).join(" and ")} ${card.loading.length === 1 ? "is" : "are"} still loading (about a minute after the app starts). This page updates by itself when ready.` })));
      loadingRetry = setTimeout(() => { if (!$("#page-today").hidden) loadCard({ quiet: true }); }, 30000);
    }
    renderGames();
    renderUnmodeled(card);
  } catch (error) {
    if (seq !== cardSeq) return;
    banner.replaceChildren(el("div", { class: "banner" }, el("span", { text: `Couldn't build the card: ${error.message}` })));
    $("#card-games").replaceChildren();
  } finally {
    clearInterval(timer);
    refresh.disabled = false;
  }
}

function renderCardSummary(card) {
  const sports = Object.values(card.by_sport);
  const withMarket = sports.reduce((n, s) => n + s.with_market, 0);
  const modeled = sports.reduce((n, s) => n + s.modeled, 0);
  const some = !card.headline.startsWith("NO");
  $("#card-summary").replaceChildren(
    el("span", { class: `headline ${some ? "some" : "none"}`, text: some ? card.headline.toLowerCase().replace(/^./, (c) => c.toUpperCase()) : "No qualified bets today" }),
    el("span", { class: "muted", text: `${withMarket} game${withMarket === 1 ? "" : "s"} with a live market · ${modeled} priced by a model` }),
    card.bettable_books ? el("span", { class: "muted small", text: `Best prices from your books: ${card.bettable_books.join(", ")}` })
      : el("span", { class: "small", style: "color:var(--warn)", text: "Best prices use every book, including exchanges. Set TTK_BETTABLE_BOOKS to your own books." }),
  );
}

function renderUnmodeled(card) {
  const box = $("#card-unmodeled");
  const items = [
    ...card.unmodeled.map((u) => `${sportName(u.sport)} · ${u.matchup} · ${eastern(u.commence_time)}: ${u.reason}`),
    ...card.unbettable.map((side) => `${side}: none of your books offers this line`),
  ];
  box.hidden = !items.length;
  $("summary", box).textContent = `${items.length} game${items.length === 1 ? "" : "s"} without a model price`;
  $("ul", box).replaceChildren(...items.map((t) => el("li", { text: t })));
}

loaders.today = () => { if (!lastCard || lastCard.date !== cardDate.value) loadCard(); else renderGames(); };

// ------------------------------------------------------------------ record a bet

const betDialog = $("#bet-dialog");
const betForm = $("#bet-form");
const bet = { game: null, market: "SPREAD", offers: [], maxAllowed: undefined, showAlternates: false };

function splitMatchup(matchup) {
  const [away, home] = matchup.split(" @ ");
  return { away, home };
}

async function openBetDialog(prefill = {}) {
  betForm.reset();
  $("#bet-override-field").hidden = true;
  setStatus($("#bet-form-status"), "");
  bet.game = null;
  bet.offers = [];
  bet.showAlternates = false;
  $("#bet-date").value = prefill.game ? easternDateOf(prefill.game.commence_time) : todayEastern();
  if (prefill.sportsbook) $("#bet-book").value = prefill.sportsbook;
  setBetMarket(prefill.market || "SPREAD");
  betDialog.showModal();
  loadBetLimits();
  if (prefill.game) {
    await chooseGame(prefill.game);
    if (prefill.selection) $("#bet-selection").value = prefill.selection;
    if (prefill.line != null) $("#bet-line").value = prefill.line;
    if (prefill.american_odds != null) $("#bet-odds").value = Math.round(prefill.american_odds);
    if (prefill.stake) $("#bet-stake").value = prefill.stake.toFixed(2);
    const isMain = bet.offers.some((o) => o.main && o.market === bet.market && o.selection === prefill.selection && o.line === prefill.line);
    if (!isMain) { bet.showAlternates = true; renderBetOffers(); }
    markChosenOffer();
    $("#bet-stake").focus();
    setStatus($("#bet-form-status"), "Check the odds match what you actually got.");
  } else {
    showGamePicker();
    loadBetGames();
    $("#bet-game-search").focus();
  }
  updateBetSubmit();
}

function showGamePicker() {
  $("#bet-game-picker").hidden = false;
  $("#bet-chosen").hidden = true;
  $("#bet-details").hidden = true;
}

let betGames = [];
async function loadBetGames() {
  const list = $("#bet-game-results");
  list.replaceChildren(el("li", { class: "muted small", text: "Loading games…" }));
  try {
    betGames = await api(`/api/games?date=${encodeURIComponent($("#bet-date").value || todayEastern())}`);
    renderBetGames();
  } catch (error) {
    list.replaceChildren(el("li", { class: "muted small", text: `Couldn't load games: ${error.message}` }));
  }
}
function renderBetGames() {
  const q = $("#bet-game-search").value.trim().toLowerCase();
  const shown = betGames.filter((g) => !q || `${g.away_team} ${g.home_team}`.toLowerCase().includes(q)).slice(0, 60);
  const list = $("#bet-game-results");
  if (!shown.length) {
    list.replaceChildren(el("li", { class: "muted small", text: betGames.length ? "No game matches that name." : "No upcoming games on this date." }));
    return;
  }
  list.replaceChildren(...shown.map((g) => {
    const b = el("button", { type: "button" },
      el("span", { text: `${g.away_team} @ ${g.home_team}` }),
      el("span", { text: `${sportName(g.sport)} · ${eastern(g.commence_time)}` }));
    b.addEventListener("click", () => chooseGame({
      id: g.id, matchup: `${g.away_team} @ ${g.home_team}`, commence_time: g.commence_time, sport: g.sport,
    }));
    return el("li", {}, b);
  }));
}
$("#bet-date").addEventListener("change", loadBetGames);
$("#bet-game-search").addEventListener("input", renderBetGames);
$("#bet-change-game").addEventListener("click", () => { bet.game = null; showGamePicker(); loadBetGames(); updateBetSubmit(); });

async function chooseGame(game) {
  bet.game = game;
  $("#bet-game-picker").hidden = true;
  $("#bet-chosen").hidden = false;
  $("#bet-details").hidden = false;
  $("#bet-chosen-game").textContent = game.matchup;
  $("#bet-chosen-time").textContent = `${sportName(game.sport)} · ${eastern(game.commence_time)}`;
  fillSelections();
  await loadBetOffers();
  updateBetSubmit();
}

function fillSelections() {
  if (!bet.game) return;
  const { away, home } = splitMatchup(bet.game.matchup);
  const opts = bet.market === "TOTAL"
    ? [["OVER", "Over"], ["UNDER", "Under"]]
    : [["HOME", home], ["AWAY", away]];
  $("#bet-selection").replaceChildren(...opts.map(([v, t]) => el("option", { value: v, text: t })));
  $("#bet-line").disabled = bet.market === "MONEYLINE";
  if (bet.market === "MONEYLINE") $("#bet-line").value = "";
}

function setBetMarket(market) {
  bet.market = market;
  bet.showAlternates = false;
  for (const b of $$("#bet-market button")) b.setAttribute("aria-checked", String(b.dataset.market === market));
  fillSelections();
  renderBetOffers();
}
for (const b of $$("#bet-market button")) b.addEventListener("click", () => setBetMarket(b.dataset.market));
$("#bet-book").addEventListener("change", loadBetOffers);

let offersSeq = 0;
async function loadBetOffers() {
  if (!bet.game) return;
  const seq = ++offersSeq;
  const box = $("#bet-offers");
  box.replaceChildren(el("span", { class: "muted small", text: "Loading this book's lines…" }));
  try {
    const offers = await api(`/api/games/${bet.game.id}/offers?book=${encodeURIComponent($("#bet-book").value)}`);
    if (seq !== offersSeq) return;
    bet.offers = offers;
  } catch {
    if (seq !== offersSeq) return;
    bet.offers = [];
  }
  renderBetOffers();
}

function offerLabel(o, matchup) {
  const { away, home } = splitMatchup(matchup);
  if (o.market === "TOTAL") return `${o.selection === "OVER" ? "Over" : "Under"} ${o.line}`;
  const team = o.selection === "HOME" ? home : away;
  return o.market === "MONEYLINE" ? `${team} to win` : `${team} ${lineText(o.line)}`;
}

function renderBetOffers() {
  const box = $("#bet-offers");
  if (!bet.game) { box.replaceChildren(); return; }
  const offers = bet.offers.filter((o) => o.market === bet.market).sort((a, b) => Number(b.main) - Number(a.main));
  if (!offers.length) {
    box.replaceChildren(el("span", { class: "muted small", text: `${$("#bet-book").value} has no ${MARKET_NAMES[bet.market].toLowerCase()} lines for this game right now. You can still type the bet in.` }));
    return;
  }
  // The book's main lines first; alternates (sorted nearest the main line) on request.
  const main = offers.filter((o) => o.main);
  const mainLine = (sel) => main.find((m) => m.selection === sel)?.line;
  const alternates = offers.filter((o) => !o.main).sort((a, b) =>
    Math.abs((a.line ?? 0) - (mainLine(a.selection) ?? 0)) - Math.abs((b.line ?? 0) - (mainLine(b.selection) ?? 0)));
  const visible = bet.showAlternates || !main.length ? [...main, ...alternates] : main;
  const buttons = visible.map((o) => {
    const b = el("button", { type: "button", class: `offer ${o.main ? "main" : ""}`, "aria-pressed": "false",
      "data-sel": o.selection, "data-line": o.line ?? "" },
      el("span", { text: offerLabel(o, bet.game.matchup) }),
      el("span", { class: "odds", text: `${american(o.american_odds)}${o.main ? " · main line" : ""}` }));
    b.addEventListener("click", () => {
      $("#bet-selection").value = o.selection;
      $("#bet-line").value = o.line ?? "";
      $("#bet-odds").value = Math.round(o.american_odds);
      markChosenOffer();
      updateBetSubmit();
      $("#bet-stake").focus();
    });
    return b;
  });
  if (!bet.showAlternates && main.length && alternates.length) {
    const more = el("button", { type: "button", class: "ghost small-btn", text: `Show ${alternates.length} alternate lines` });
    more.addEventListener("click", () => { bet.showAlternates = true; renderBetOffers(); });
    buttons.push(more);
  }
  box.replaceChildren(...buttons);
  markChosenOffer();
}

function markChosenOffer() {
  const sel = $("#bet-selection").value;
  const line = $("#bet-line").value;
  for (const b of $$("#bet-offers .offer")) {
    b.setAttribute("aria-pressed", String(b.dataset.sel === sel && String(b.dataset.line) === String(line)));
  }
}

async function loadBetLimits() {
  try {
    const b = await api("/api/bankroll");
    bet.maxAllowed = b.configured ? b.max_allowed : null;
  } catch { bet.maxAllowed = null; }
  updateStakeHint();
}
function updateStakeHint() {
  const hint = $("#bet-stake-hint");
  const stake = Number($("#bet-stake").value);
  if (bet.maxAllowed === undefined) { hint.textContent = ""; return; }
  if (bet.maxAllowed === null) { hint.textContent = "No bankroll set up, so no limits apply."; hint.className = "hint"; return; }
  const over = stake > bet.maxAllowed + 1e-9;
  hint.textContent = over ? `Over your limits (up to ${money(bet.maxAllowed)} right now).` : `Your limits allow up to ${money(bet.maxAllowed)}.`;
  hint.className = `hint ${over ? "over" : ""}`;
}

function updateBetSubmit() {
  $("#bet-submit").disabled = !(bet.game && $("#bet-odds").value !== "" && Number($("#bet-stake").value) > 0);
}
for (const id of ["bet-odds", "bet-stake", "bet-line"]) {
  $(`#${id}`).addEventListener("input", () => { updateBetSubmit(); updateStakeHint(); markChosenOffer(); });
}
$("#bet-selection").addEventListener("change", markChosenOffer);
$("#bet-dialog-close").addEventListener("click", () => betDialog.close());
$("#bet-cancel").addEventListener("click", () => betDialog.close());
$("#new-bet").addEventListener("click", () => openBetDialog());

betForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = $("#bet-form-status");
  const body = {
    game_id: bet.game.id,
    market: bet.market,
    selection: $("#bet-selection").value,
    line: bet.market === "MONEYLINE" || $("#bet-line").value === "" ? null : Number($("#bet-line").value),
    american_odds: Number($("#bet-odds").value),
    sportsbook: $("#bet-book").value,
    stake: Number($("#bet-stake").value),
    placed_at: betForm.placed_at.value ? new Date(betForm.placed_at.value).toISOString() : null,
    notes: betForm.notes.value || null,
    limit_override: betForm.limit_override.value || null,
  };
  $("#bet-submit").disabled = true;
  try {
    const saved = await api("/api/bets", { method: "POST", body: JSON.stringify(body) });
    betDialog.close();
    toast(`Recorded: ${saved.description} at ${saved.american_odds}`);
    if (!$("#page-bets").hidden) { loadBetList(); loadParlayList(); }
  } catch (error) {
    if (/bankroll limit/i.test(error.message)) $("#bet-override-field").hidden = false;
    setStatus(status, `Not recorded: ${error.message}`, "err");
  } finally {
    updateBetSubmit();
  }
});

function setStatus(node, text, kind = "") {
  node.textContent = text;
  node.className = `form-status ${kind}`;
}

// ------------------------------------------------------------------ my bets

let betStatusFilter = "";
const SUBS = ["list", "bankroll", "results"];
loaders.bets = (sub) => {
  const name = SUBS.includes(sub) ? sub : "list";
  for (const b of $$(".segmented[aria-label='My bets sections'] button")) b.setAttribute("aria-selected", String(b.dataset.sub === name));
  for (const s of SUBS) $(`#sub-${s}`).hidden = s !== name;
  if (name === "list") { loadBetList(); loadParlayList(); }
  if (name === "bankroll") loadBankroll();
  if (name === "results") loadPerformance();
};
for (const b of $$(".segmented[aria-label='My bets sections'] button")) {
  b.addEventListener("click", () => go(b.dataset.sub === "list" ? "bets" : `bets/${b.dataset.sub}`));
}
for (const pill of $$("#bet-pills .pill")) {
  pill.addEventListener("click", () => { betStatusFilter = pill.dataset.status; pressPill($("#bet-pills"), pill); loadBetList(); });
}

async function loadBetList() {
  const box = $("#bet-list");
  try {
    const bets = await api(`/api/bets${betStatusFilter ? `?status=${betStatusFilter}` : ""}`);
    if (!bets.length) {
      const add = el("button", { type: "button", text: "Record a bet" });
      add.addEventListener("click", () => openBetDialog());
      box.replaceChildren(empty(betStatusFilter ? "Nothing here" : "No bets recorded yet",
        "After you place a bet, record it here, or use Track bet on Today's games. It's graded automatically when the game ends.", add));
      return;
    }
    box.replaceChildren(...bets.map(betRow));
  } catch (error) {
    box.replaceChildren(empty("Couldn't load your bets", error.message));
  }
}

function betRow(b) {
  const voidButton = el("button", { type: "button", class: "ghost small-btn", text: "Void" });
  voidButton.addEventListener("click", async () => {
    if (!confirm(`Void this bet? ${b.description}`)) return;
    await api(`/api/bets/${b.id}`, { method: "PATCH", body: JSON.stringify({ void: true }) });
    toast("Bet voided");
    loadBetList();
  });
  const beliefs = b.model_probability != null
    ? `model ${pct(b.model_probability)} vs market ${pct(b.market_probability)}` : "no model price at this line";
  return el("div", { class: "bet-row" },
    el("div", { class: "what" },
      el("strong", { text: b.description }),
      el("span", { class: "meta", text: `${b.american_odds} at ${b.sportsbook ?? "?"} · ${new Date(b.placed_at).toLocaleString()} · ${beliefs}` }),
      b.clv != null ? el("span", { class: `meta ${signClass(b.clv)}` }, `Beat the closing price by ${signedPct(b.clv)} `, term("clv")) : null),
    el("div", { class: "money" },
      el("span", { text: money(b.stake) }),
      b.profit_loss != null ? el("span", { class: signClass(b.profit_loss), text: signedMoney(b.profit_loss) }) : el("span", { class: "muted small", text: "staked" })),
    el("div", { class: "row-actions" },
      el("span", { class: `chip ${b.result}`, text: b.result === "PENDING" ? "waiting" : b.result.toLowerCase() }),
      b.result === "PENDING" ? voidButton : null));
}

async function loadParlayList() {
  const box = $("#parlay-list");
  try {
    const parlays = await api("/api/parlays");
    if (!parlays.length) {
      box.replaceChildren(el("p", { class: "empty-note", text: "No parlays recorded. Build one in the Parlay builder." }));
      return;
    }
    box.replaceChildren(...parlays.map((p) => el("div", { class: "bet-row" },
      el("div", { class: "what" },
        el("strong", { text: `${p.legs.length}-leg parlay at ${p.sportsbook ?? "?"} (${p.american_odds ?? "?"})` }),
        ...p.legs.map((leg) => el("span", { class: "meta", text: `${leg.result === "PENDING" ? "·" : leg.result === "WIN" ? "✓" : leg.result === "LOSS" ? "✗" : "–"} ${leg.description} ${leg.american_odds}` }))),
      el("div", { class: "money" },
        el("span", { text: money(p.stake) }),
        p.profit_loss != null ? el("span", { class: signClass(p.profit_loss), text: signedMoney(p.profit_loss) }) : el("span", { class: "muted small", text: `${pct(p.joint_probability, 1)} to hit` })),
      el("div", { class: "row-actions" }, el("span", { class: `chip ${p.result}`, text: p.result === "PENDING" ? "waiting" : p.result.toLowerCase() })))));
  } catch (error) {
    box.replaceChildren(el("p", { class: "empty-note", text: `Couldn't load parlays: ${error.message}` }));
  }
}

$("#bets-settle").addEventListener("click", async () => {
  const status = $("#bets-status");
  try {
    const settled = await api("/api/bets/settle", { method: "POST" });
    const n = settled.bets.length + settled.parlays.length;
    setStatus(status, n ? `${n} graded` : "Nothing new to grade", "ok");
    loadBetList();
    loadParlayList();
  } catch (error) {
    setStatus(status, `Couldn't grade: ${error.message}`, "err");
  }
});

// bankroll
const POLICY_PERCENT = ["max_stake_fraction", "max_daily_fraction", "max_open_fraction", "stop_drawdown_fraction"];
async function loadBankroll() {
  const b = await api("/api/bankroll");
  const p = b.policy;
  $("#bankroll-note").textContent = b.configured
    ? `Limits${p.saved_at ? "" : " (the defaults)"}: at most ${pct(p.max_stake_fraction)} on one bet, ${pct(p.max_daily_fraction, 0)} a day and ${pct(p.max_open_fraction, 0)} riding at once; bets pause after a ${pct(p.stop_drawdown_fraction, 0)} drop from the peak. Going over a limit needs a reason.`
    : "Not set up yet. Record a deposit with your starting amount to turn on staking limits.";
  $("#bankroll-tiles").replaceChildren(...(b.configured ? [
    metric("Balance", money(b.balance), `deposits ${money(b.net_deposits)} · betting ${signedMoney(b.realized_profit)}`),
    metric("From the peak", signedPct(b.drawdown), `peak ${money(b.peak)}`, b.drawdown < 0 ? "neg" : "", "drawdown"),
    metric("Riding now", money(b.open_exposure), `${b.open_wagers} open bet${b.open_wagers === 1 ? "" : "s"}`),
    metric("Staked today", money(b.staked_today), "Eastern time"),
    metric("Largest bet allowed now", money(b.max_allowed ?? 0), "under every limit", b.max_allowed > 0 ? "" : "neg"),
  ] : []));
  const form = $("#bankroll-policy");
  form.kelly_multiplier.value = p.kelly_multiplier;
  for (const name of POLICY_PERCENT) form[name].value = +(p[name] * 100).toFixed(2);
}
$("#bankroll-entry").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/bankroll/entries", { method: "POST", body: JSON.stringify({
      kind: form.kind.value, amount: Number(form.amount.value), note: form.note.value || null,
    }) });
    form.reset();
    toast("Saved");
    loadBankroll();
  } catch (error) {
    setStatus($("#bankroll-status"), `Not saved: ${error.message}`, "err");
  }
});
$("#bankroll-policy").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = { kelly_multiplier: Number(form.kelly_multiplier.value) };
  for (const name of POLICY_PERCENT) body[name] = Number(form[name].value) / 100;
  try {
    await api("/api/bankroll/policy", { method: "PUT", body: JSON.stringify(body) });
    toast("Limits saved");
    loadBankroll();
  } catch (error) {
    setStatus($("#bankroll-status"), `Limits not saved: ${error.message}`, "err");
  }
});

// results
async function loadPerformance() {
  const params = new URLSearchParams();
  if ($("#perf-sport").value) params.set("sport", $("#perf-sport").value);
  if ($("#perf-market").value) params.set("market", $("#perf-market").value);
  const box = $("#perf-body");
  const p = await api(`/api/performance${params.size ? `?${params}` : ""}`);
  const pp = p.parlays;
  if (!p.bets && !p.pending && !pp.parlays && !pp.pending) {
    box.replaceChildren(empty("No results yet",
      "Once your recorded bets are graded, this shows your record, profit, return and, most telling, whether you beat the closing prices."));
    return;
  }
  box.replaceChildren(
    el("div", { class: "metric-row" },
      metric("Record", `${p.wins}-${p.losses}-${p.pushes}`, `${p.bets} graded · ${p.pending} waiting`),
      metric("Win rate", pct(p.hit_rate), "pushes left out", "", "winrate"),
      metric("Profit", signedMoney(p.profit), `on ${money(p.staked)} staked`, signClass(p.profit)),
      metric("Return", signedPct(p.roi), `${p.bets} bets`, signClass(p.roi), "roi"),
      metric("Beat the close by", signedPct(p.avg_clv), `${p.clv_n} bets`, signClass(p.avg_clv), "clv"),
      metric("Average edge", pts(p.avg_edge), `${p.edge_n} bets`, signClass(p.avg_edge), "edge"),
      metric("Worst drop", money(p.max_drawdown), "peak to low point", p.max_drawdown < 0 ? "neg" : ""),
      metric("Riding now", money(p.pending_stake), `${p.pending} waiting`)),
    el("h3", { text: "Parlays" }),
    el("div", { class: "metric-row" },
      metric("Record", `${pp.wins}-${pp.losses}-${pp.pushes}`, `${pp.parlays} graded · ${pp.pending} waiting`),
      metric("Profit", signedMoney(pp.profit), `on ${money(pp.staked)} staked`, signClass(pp.profit)),
      metric("Return", signedPct(pp.roi), `${pp.parlays} parlays`, signClass(pp.roi))),
    el("p", { class: "muted small", text: "Every figure shows how many bets it rests on. A small sample says little; beating the closing price over many bets is the best evidence of an edge." }),
  );
}
$("#perf-sport").addEventListener("change", loadPerformance);
$("#perf-market").addEventListener("change", loadPerformance);

// ------------------------------------------------------------------ parlay builder

const SLIP_KEY = "ttk-slip";
const slip = (() => {
  try {
    const saved = JSON.parse(localStorage.getItem(SLIP_KEY) || "null");
    if (saved && Array.isArray(saved.legs)) return saved;
  } catch { /* start empty */ }
  return { book: null, legs: [] };
})();

function persistSlip() {
  try { localStorage.setItem(SLIP_KEY, JSON.stringify(slip)); } catch { /* not remembered */ }
  $("#slip-count").textContent = slip.legs.length ? String(slip.legs.length) : "";
}
function setSlipBook(book) {
  slip.book = book;
  $("#slip-book").value = book;
  persistSlip();
  analyze();
}
function addLeg(leg) {
  const same = (a) => a.game_id === leg.game_id && a.market === leg.market && a.selection === leg.selection && a.line === leg.line;
  if (!slip.legs.some(same)) slip.legs.push(leg);
  persistSlip();
  renderSlip();
}
function removeLeg(index) {
  slip.legs.splice(index, 1);
  persistSlip();
  renderSlip();
}

function renderSlip() {
  $("#slip-legs").replaceChildren(...slip.legs.map((leg, i) => {
    const remove = el("button", { type: "button", class: "ghost small-btn", text: "Remove", "aria-label": `Remove leg ${i + 1}` });
    remove.addEventListener("click", () => removeLeg(i));
    const odds = el("input", { type: "number", placeholder: "your odds", "aria-label": `Odds you were offered for leg ${i + 1} (optional)`, value: leg.american_odds ?? "" });
    odds.addEventListener("change", () => {
      leg.american_odds = odds.value === "" ? null : Number(odds.value);
      persistSlip();
      analyze();
    });
    return el("li", {}, el("span", { text: leg.label }), odds, remove);
  }));
  $("#slip-empty").hidden = slip.legs.length > 0;
  analyze();
}

let analyzeSeq = 0;
async function analyze() {
  const seq = ++analyzeSeq;
  const status = $("#lab-status");
  const clear = () => { $("#lab-summary").replaceChildren(); $("#lab-warnings").replaceChildren(); $("#lab-legs").replaceChildren(); };
  if (slip.legs.length < 2 || !slip.book) { clear(); status.textContent = "Add at least two legs."; return; }
  status.textContent = "Working it out…";
  try {
    const body = { sportsbook: slip.book, legs: slip.legs.map(({ label, ...leg }) => leg) };
    const a = await api("/api/parlays/evaluate", { method: "POST", body: JSON.stringify(body) });
    if (seq !== analyzeSeq) return;
    status.textContent = `${a.legs.length} legs at ${a.sportsbook}.`;
    $("#lab-summary").replaceChildren(
      metric("Parlay price", american(a.american_odds), `the book says ${pct(a.book_implied_probability)}`),
      metric("Chance all legs win", pct(a.joint_probability, 2), a.correlations.length ? "same-game legs simulated" : "independent legs", "", "joint"),
      metric("Fair price", american(a.fair_american_odds), a.market_joint_probability == null ? "" : `market says ${pct(a.market_joint_probability, 2)}`, "", "fair"),
      metric("Expected value", signedPct(a.ev_per_unit), "per $1 staked", signClass(a.ev_per_unit), "ev"),
      metric("Same-game risk", a.correlation_risk.toLowerCase(), `${a.correlations.length} linked pair${a.correlations.length === 1 ? "" : "s"}`, a.correlation_risk === "LOW" ? "" : "neg"),
    );
    $("#lab-warnings").replaceChildren(
      ...a.warnings.map((w) => el("li", { text: w })),
      ...a.correlations.map((c) => el("li", { text: `Legs ${c.legs[0] + 1} and ${c.legs[1] + 1} (${c.risk.toLowerCase()}): ${c.reason}` })),
    );
    const impact = Object.fromEntries(a.impacts.map((i) => [i.index, i.ev_without]));
    $("#lab-legs").replaceChildren(...a.legs.map((leg) => {
      const tags = [];
      const tag = (text, cls = "") => tags.push(el("span", { class: `tag ${cls}`, text }));
      if (leg.index === a.strongest_leg) tag("strongest", "good");
      if (leg.index === a.weakest_leg) tag("weakest", "bad");
      if (leg.index === a.lowest_edge_leg && leg.edge != null) tag("smallest edge", "bad");
      if (leg.index === a.highest_correlation_leg) tag("most linked", "bad");
      if (leg.index === a.reduces_ev_most) tag(`dropping it: ${signedPct(impact[leg.index])} value`, "bad");
      const age = leg.odds_age_minutes == null ? leg.price_source : `${Math.round(leg.odds_age_minutes)} min old`;
      return el("div", { class: "leg" },
        el("div", { class: "top" }, el("span", { text: leg.description }), el("span", { class: "mono", text: american(leg.american_odds) })),
        el("div", { class: "meta" },
          el("span", { text: `${sportName(leg.sport)} · ${leg.matchup} · ${eastern(leg.commence_time)}` }),
          el("span", { text: `${pct(leg.probability)} to win (${leg.probability_source})` }),
          el("span", { class: signClass(leg.ev_per_unit), text: `value ${signedPct(leg.ev_per_unit)}` }),
          el("span", { text: `price ${age}` })),
        tags.length ? el("div", {}, ...tags) : null);
    }));
  } catch (error) {
    if (seq !== analyzeSeq) return;
    clear();
    status.textContent = error.message;
  }
}

$("#slip-book").addEventListener("change", (event) => setSlipBook(event.target.value));
$("#slip-clear").addEventListener("click", () => {
  if (slip.legs.length && !confirm("Remove every leg from the slip?")) return;
  slip.legs = [];
  persistSlip();
  renderSlip();
});

let pickSeq = 0;
async function loadPickGames() {
  const seq = ++pickSeq;
  const params = new URLSearchParams({ date: $("#pick-date").value || todayEastern() });
  if ($("#pick-sport").value) params.set("sport", $("#pick-sport").value);
  const games = await api(`/api/games?${params}`);
  if (seq !== pickSeq) return;
  $("#pick-game").replaceChildren(
    el("option", { value: "", text: games.length ? "Choose a game" : "No upcoming games" }),
    ...games.map((g) => el("option", { value: g.id, "data-matchup": `${g.away_team} @ ${g.home_team}`, text: `${sportName(g.sport)} · ${g.away_team} @ ${g.home_team} · ${eastern(g.commence_time)}` })),
  );
  $("#pick-offers").replaceChildren();
}

let pickOffersSeq = 0;
async function loadPickOffers() {
  const seq = ++pickOffersSeq;
  const gameId = Number($("#pick-game").value);
  const box = $("#pick-offers");
  box.replaceChildren();
  if (!gameId || !slip.book) return;
  const matchup = $("#pick-game").selectedOptions[0]?.dataset.matchup ?? "";
  const offers = await api(`/api/games/${gameId}/offers?book=${encodeURIComponent(slip.book)}`);
  if (seq !== pickOffersSeq) return;
  if (!offers.length) { box.append(el("p", { class: "empty-note", text: `${slip.book} has no lines for this game.` })); return; }
  let group = "";
  const sorted = [...offers].sort((a, b) => a.market.localeCompare(b.market) || Number(b.main) - Number(a.main));
  for (const o of sorted) {
    if (o.market !== group) { group = o.market; box.append(el("div", { class: "group", text: MARKET_NAMES[group] ?? group })); }
    const text = offerLabel(o, matchup);
    const button = el("button", { type: "button", class: `offer ${o.main ? "main" : ""}` },
      el("span", { text }), el("span", { class: "odds", text: `${american(o.american_odds)}${o.main ? " · main line" : ""}` }));
    button.addEventListener("click", () => {
      addLeg({ game_id: gameId, market: o.market, selection: o.selection, line: o.line, label: `${text} (${matchup})` });
      toast(`Added: ${text}`);
    });
    box.append(button);
  }
}

$("#pick-date").value = todayEastern();
$("#pick-date").addEventListener("change", loadPickGames);
$("#pick-sport").addEventListener("change", loadPickGames);
$("#pick-game").addEventListener("change", loadPickOffers);

$("#parlay-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const status = $("#parlay-form-status");
  if (slip.legs.length < 2) { setStatus(status, "Add at least two legs first.", "err"); return; }
  const body = {
    sportsbook: slip.book,
    legs: slip.legs.map(({ label, ...leg }) => leg),
    stake: Number(form.stake.value),
    american_odds: form.american_odds.value === "" ? null : Number(form.american_odds.value),
    placed_at: form.placed_at.value ? new Date(form.placed_at.value).toISOString() : null,
    notes: form.notes.value || null,
    limit_override: form.limit_override.value || null,
  };
  try {
    const saved = await api("/api/parlays", { method: "POST", body: JSON.stringify(body) });
    form.reset();
    $("#parlay-override-field").hidden = true;
    setStatus(status, "");
    toast(`Parlay recorded at ${saved.american_odds}`);
  } catch (error) {
    if (/bankroll limit/i.test(error.message)) $("#parlay-override-field").hidden = false;
    setStatus(status, `Not recorded: ${error.message}`, "err");
  }
});

loaders.slip = () => { renderSlip(); loadPickGames(); };

// ------------------------------------------------------------------ simulator

let simSeq = 0;
async function loadSimGames(selectId = null) {
  const seq = ++simSeq;
  const sport = $("#sim-sport").value;
  const games = await api(`/api/games?${new URLSearchParams({ date: $("#sim-date").value || todayEastern(), sport })}`);
  if (seq !== simSeq) return;
  $("#sim-game").replaceChildren(...games.map((g) => el("option", { value: g.id, text: `${g.away_team} @ ${g.home_team} · ${eastern(g.commence_time)}` })));
  if (!games.length) $("#sim-game").append(el("option", { value: "", text: `No upcoming ${sportName(sport)} games on this date` }));
  if (selectId != null) $("#sim-game").value = String(selectId);
}

let pendingSim = null;
async function openSimulation(gameId, commenceIso, sport) {
  $("#sim-sport").value = sport;
  $("#sim-date").value = easternDateOf(commenceIso);
  pendingSim = gameId;
  go("sim");
}
loaders.sim = async () => {
  const target = pendingSim;
  pendingSim = null;
  await loadSimGames(target);
  if (target != null) runSimulation();
};

function sensitivityTable(title, rows, mainLine, fmtLine) {
  return el("div", { class: "card" },
    el("h2", { text: title }),
    el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ...["Line", "Wins", "Pushes", "Wins (pushes aside)"].map((h, i) => el("th", { class: i ? "num" : "", text: h })))),
      el("tbody", {}, ...rows.map((r) => el("tr", { class: r.line === mainLine ? "main" : "" },
        el("td", { text: fmtLine(r.line) + (r.line === mainLine ? "  (market)" : "") }),
        el("td", { class: "num", text: pct(r.win) }),
        el("td", { class: "num", text: pct(r.push) }),
        el("td", { class: "num", text: pct(r.win_excluding_push) })))))));
}

async function runSimulation() {
  const gameId = Number($("#sim-game").value);
  const status = $("#sim-status");
  const out = $("#sim-result");
  if (!gameId) { setStatus(status, "Choose a game first.", "err"); return; }
  const button = $("#sim-run");
  button.disabled = true;
  setStatus(status, "Simulating…");
  out.replaceChildren(el("div", { class: "skeleton-card" }));
  try {
    const body = { game_id: gameId, preset: $("#sim-preset").value, seed: $("#sim-seed").value === "" ? null : Number($("#sim-seed").value) };
    const s = await api("/api/simulations/run", { method: "POST", body: JSON.stringify(body) });
    setStatus(status, `${s.iterations.toLocaleString()} simulated games${s.seed == null ? "" : `, seed ${s.seed}`}`, "ok");
    const { away, home } = splitMatchup(s.matchup);
    const q = (o) => `middle half ${o.p25} to ${o.p75} · 9 in 10 between ${o.p5} and ${o.p95} · median ${o.p50}`;
    const sides = [...s.spread_sides, ...s.total_sides].filter((x) => x.american_odds != null);
    out.replaceChildren(
      el("div", { class: "metric-row" },
        metric("Typical score", `${s.home_score_mean.toFixed(0)}–${s.away_score_mean.toFixed(0)}`, `${home} – ${away}`),
        metric(`${home} win`, pct(s.home_win.value), `± ${pct(s.home_win.standard_error, 2)} from chance${s.tie.value ? ` · tie ${pct(s.tie.value)}` : ""}`),
        metric("Total centered on", String(s.total_line), s.total_line_source)),
      el("div", { class: "card" },
        el("h2", { text: "How the score is spread out" }),
        el("p", { class: "small", text: `${home} margin: ${q(s.margin_quantiles)}` }),
        el("p", { class: "small", text: `Total points: ${q(s.total_quantiles)}` })),
      el("div", { class: "sim-grid" },
        sensitivityTable(`${home} at each spread`, s.spread_sensitivity, s.spread_sides[0].line, lineText),
        sensitivityTable("The over at each total", s.total_sensitivity, s.total_line, (x) => String(x)),
        el("div", { class: "card" },
          el("h2", {}, "Worst line still worth taking ", term("maxline")),
          el("ul", { class: "notes" }, ...sides.map((x) => {
            const who = x.selection === "HOME" ? home : x.selection === "AWAY" ? away : x.selection === "OVER" ? "Over" : "Under";
            const line = x.selection === "OVER" || x.selection === "UNDER" ? String(x.line) : lineText(x.line);
            const worst = x.max_acceptable_line == null ? "no line in range is worth it"
              : x.selection === "OVER" ? `${x.max_acceptable_line} or lower`
              : x.selection === "UNDER" ? `${x.max_acceptable_line} or higher`
              : `${lineText(x.max_acceptable_line)} or better`;
            return el("li", { text: `${who} ${line} at ${american(x.american_odds)} (${x.sportsbook}): ${worst}` });
          }))),
        el("div", { class: "card" },
          el("h2", { text: "Spread and total together (main lines)" }),
          el("ul", { class: "notes" }, ...Object.entries(s.joint).map(([k, p]) =>
            el("li", { text: `${k.replaceAll("_", " ").replace("home", home).replace("away", away)}: ${pct(p.value)}` }))))),
    );
  } catch (error) {
    setStatus(status, `Couldn't simulate: ${error.message}`, "err");
    out.replaceChildren();
  } finally {
    button.disabled = false;
  }
}
$("#sim-date").value = todayEastern();
$("#sim-date").addEventListener("change", () => loadSimGames());
$("#sim-sport").addEventListener("change", () => loadSimGames());
$("#sim-run").addEventListener("click", runSimulation);

// ------------------------------------------------------------------ model report

let modelSport = "";
let modelData = null;
for (const pill of $$("#model-pills .pill")) {
  pill.addEventListener("click", () => { modelSport = pill.dataset.sport; pressPill($("#model-pills"), pill); renderModels(); loadLab(""); });
}

loaders.models = async () => {
  $("#model-cards").replaceChildren(...[1, 2, 3].map(() => el("div", { class: "skeleton-card" })));
  try {
    const [status, forward] = await Promise.all([api("/api/status"), api("/api/forward")]);
    modelData = { status, forward };
    renderModels();
    renderRecent(forward);
  } catch (error) {
    $("#model-cards").replaceChildren(empty("Couldn't load the model report", error.message));
  }
  loadLab();
};

function renderModels() {
  if (!modelData) return;
  const { status, forward } = modelData;
  const models = sortModels(status.models).filter((m) => !modelSport || m.sport === modelSport);
  $("#model-cards").replaceChildren(...models.map((m) => {
    const n = modelName(m.model);
    const state = modelState(m, status);
    const share = Math.min(1, m.decided / status.min_decided);
    const scores = forward.scores.filter((s) => s.model === m.model);
    const table = scores.length ? el("details", {},
      el("summary", { text: "Details by timing" }),
      el("div", { class: "table-wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, el("th", { text: "Before kickoff" }), el("th", { class: "num", text: "Games" }),
          el("th", { class: "num" }, "vs market ", term("vsmarket")), el("th", { class: "num" }, "CLV ", term("clv")),
          el("th", { text: "Edge ≥ 2 pts" }))),
        el("tbody", {}, ...scores.map((s) => {
          const two = s.bets.find((b) => Math.abs(b.min_edge - 0.02) < 1e-9);
          return el("tr", {},
            el("td", { text: `${s.horizon_hours} h` }),
            el("td", { class: "num", text: `${s.decided}` }),
            el("td", { class: `num ${signClass(s.paired_diff == null ? null : -s.paired_diff)}`, text: s.paired_diff == null ? "–" : `${s.paired_diff >= 0 ? "+" : ""}${s.paired_diff.toFixed(4)}` }),
            el("td", { class: `num ${signClass(s.price_clv)}`, text: signedPct(s.price_clv) }),
            el("td", { text: two && two.bets ? `${two.wins}-${two.losses}-${two.pushes}, ${signedPct(two.roi)}` : "–" }));
        }))))) : null;
    return el("article", { class: "model-card" },
      el("div", { class: "title" }, el("strong", { text: n.title }), el("span", { text: n.detail || m.model })),
      el("div", { class: "filter-row" },
        el("span", { class: "chip sport", text: sportName(m.sport) }),
        el("span", { class: "chip", text: MARKET_NAMES[m.market] ?? m.market }),
        el("span", { class: "chip warn", text: m.status === "DEVELOPMENT" ? "in testing" : m.status.toLowerCase() }), term("testing")),
      el("div", { class: "bar-track", role: "img", "aria-label": `${m.decided} of ${status.min_decided} games` },
        el("div", { class: "bar-fill", style: `width:${(share * 100).toFixed(1)}%` })),
      el("p", { class: `verdict ${state.cls}`, text: state.text }),
      el("p", { class: "muted small", text: m.snapshots ? `${m.snapshots} prediction${m.snapshots === 1 ? "" : "s"} recorded so far.` : "No predictions recorded yet." }),
      table);
  }));
  if (!models.length) $("#model-cards").replaceChildren(empty("No models for this sport", "Pick another sport."));
}

function renderRecent(f) {
  $("#fwd-recent tbody").replaceChildren(...f.recent.map((r) => el("tr", {},
    el("td", { text: new Date(r.snapshot_at).toLocaleString() }),
    el("td", { text: modelName(r.model).title }),
    el("td", { text: `${sportName(r.sport)} · ${r.matchup}` }),
    el("td", { text: `${r.horizon_hours} h` }),
    el("td", { text: r.market === "MONEYLINE" ? `to win (spread ${lineText(r.home_line)})` : r.market === "TOTAL" ? `total ${r.home_line}` : lineText(r.home_line) }),
    el("td", { class: "num", text: pct(r.model_home_cover) }),
    el("td", { class: "num", text: pct(r.market_home_cover) }),
    el("td", { class: `num ${signClass(r.edge)}`, text: pts(r.edge) }),
    el("td", { text: r.result == null ? "waiting" : r.result === "push" ? "push" : r.market === "TOTAL" ? `went ${r.result}` : `${r.result} ${r.market === "MONEYLINE" ? "won" : "covered"}` }))));
  if (!f.recent.length) $("#fwd-recent tbody").replaceChildren(el("tr", {}, el("td", { colspan: "9", class: "muted", text: "No predictions recorded yet." })));
}

const fixed = (x, digits) => (x == null ? "–" : x.toFixed(digits));
const signedNum = (x, digits) => (x == null ? "–" : `${x >= 0 ? "+" : ""}${x.toFixed(digits)}`);
const cell = (text, cls = "") => el("td", { class: cls, text });

async function loadLab(pick = $("#lab-pick").value) {
  const params = new URLSearchParams();
  if (modelSport) params.set("sport", modelSport);
  if (pick) {
    const [model, horizon] = JSON.parse(pick);
    params.set("model", model);
    params.set("horizon_hours", horizon);
  }
  let res;
  try { res = await api(`/api/lab?${params}`); } catch { return; }
  const { choices, lab } = res;
  const select = $("#lab-pick");
  select.replaceChildren(...choices.map((c) => el("option", {
    value: JSON.stringify([c.model, c.horizon_hours]),
    text: `${modelName(c.model).title} (${modelName(c.model).detail}) · ${c.horizon_hours} h before · ${c.games} games`,
  })));
  for (const t of ["#lab-thresholds", "#lab-calibration", "#lab-weeks"]) $(`${t} tbody`).replaceChildren();
  if (!lab) { $("#lab-note").textContent = "No finished games with recorded predictions yet."; return; }
  select.value = JSON.stringify([lab.model, lab.horizon_hours]);
  $("#lab-note").textContent = lab.note;
  $("#lab-thresholds tbody").replaceChildren(...lab.thresholds.map((t) => el("tr", {},
    cell(`${(t.min_probability * 100).toFixed(0)}%`),
    cell(String(t.bets), "num"),
    cell(`${t.wins}-${t.losses}-${t.pushes}`),
    cell(t.roi == null ? "–" : `${signedPct(t.roi)} ± ${pct(t.roi_se)}`, `num ${signClass(t.roi)}`),
    cell(pct(t.hit_rate), "num"),
    cell(pct(t.break_even), "num"),
    cell(signedPct(t.avg_clv, 2), `num ${signClass(t.avg_clv)}`),
    cell(t.enough ? "" : "too few to read", "muted"))));
  const market = lab.calibration.market;
  $("#lab-calibration tbody").replaceChildren(...lab.calibration.model.map((b, i) => el("tr", {},
    cell(`${(b.low * 100).toFixed(0)}–${(b.high * 100).toFixed(0)}%`),
    cell(String(b.n), "num"), cell(pct(b.mean_predicted), "num"), cell(pct(b.observed), "num"),
    cell(String(market[i].n), "num"), cell(pct(market[i].mean_predicted), "num"), cell(pct(market[i].observed), "num"))));
  $("#lab-weeks tbody").replaceChildren(...lab.weeks.map((w) => el("tr", {},
    cell(w.week), cell(String(w.games), "num"),
    cell(signedNum(w.diff, 4), `num ${signClass(w.diff == null ? null : -w.diff)}`),
    cell(signedNum(w.cumulative_diff, 4), `num ${signClass(w.cumulative_diff == null ? null : -w.cumulative_diff)}`),
    cell(signedPct(w.avg_clv, 2), `num ${signClass(w.avg_clv)}`),
    cell(signedPct(w.cumulative_clv, 2), `num ${signClass(w.cumulative_clv)}`))));
}
$("#lab-pick").addEventListener("change", (event) => loadLab(event.currentTarget.value));

// ------------------------------------------------------------------ start

restoreFilters();
persistSlip();
loadBooks().catch(() => {}).finally(() => { if (!$("#page-slip").hidden) renderSlip(); });
route();
