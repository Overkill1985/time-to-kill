// Time-to-Kill UI. Plain ES module, no build step. All data is inserted with
// textContent (never innerHTML), so values from odds feeds or notes cannot inject markup.

const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child instanceof Node ? child : String(child));
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
      : detail || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return body;
}

// ------------------------------------------------------------------ formatting

const american = (n) => (n == null ? "n/a" : (Math.round(n) > 0 ? "+" : "") + Math.round(n));
const pct = (x, digits = 1) => (x == null ? "n/a" : `${(x * 100).toFixed(digits)}%`);
const signedPct = (x, digits = 1) =>
  x == null ? "n/a" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(digits)}%`;
const pts = (x) => (x == null ? "n/a" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)} pts`);
const money = (x) => (x == null ? "n/a" : `${x >= 0 ? "+" : ""}${x.toFixed(2)}`);
const lineText = (x) => (x == null ? "" : x === 0 ? "PK" : `${x > 0 ? "+" : ""}${x}`);
const signClass = (x) => (x == null ? "" : x > 0 ? "pos" : x < 0 ? "neg" : "");
const eastern = (iso) =>
  new Date(iso).toLocaleString("en-US", {
    timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "2-digit",
  }) + " ET";
const todayEastern = () =>
  new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date());

// ------------------------------------------------------------------ tabs

function showTab(name) {
  for (const button of document.querySelectorAll(".tabs button")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === name));
  }
  for (const panel of document.querySelectorAll(".tab")) {
    panel.hidden = panel.id !== `tab-${name}`;
  }
  if (name === "bets") { loadBets(); loadParlays(); }
  if (name === "performance") loadPerformance();
  if (name === "parlay") { renderSlip(); loadPickGames(); }
}

for (const button of document.querySelectorAll(".tabs button")) {
  button.addEventListener("click", () => showTab(button.dataset.tab));
}

// ------------------------------------------------------------------ today (card)

function stat(label, value, cls = "") {
  return el("div", {}, el("dt", { text: label }), el("dd", { text: value, class: cls }));
}

function renderEntry(entry) {
  const node = $("#entry-template").content.firstElementChild.cloneNode(true);
  const badge = $(".badge", node);
  badge.textContent = entry.classification.replace("_", " ");
  badge.classList.add(entry.classification);
  $(".bet", node).textContent = entry.bet;
  $(".odds", node).textContent = `${american(entry.american_odds)} @ ${entry.sportsbook}`;
  $(".matchup", node).textContent = `${entry.matchup} · ${eastern(entry.commence_time)}`;

  const bars = $(".probbar", node);
  bars.setAttribute(
    "aria-label",
    `Model ${pct(entry.model_probability)}, market ${pct(entry.market_probability)}`,
  );
  const [modelBar, marketBar] = bars.querySelectorAll(".bar span");
  modelBar.style.width = pct(entry.model_probability, 2);
  modelBar.textContent = `Model ${pct(entry.model_probability)}`;
  marketBar.style.width = pct(entry.market_probability, 2);
  marketBar.textContent = `Market ${pct(entry.market_probability)}`;

  const movement =
    entry.line_opening != null && entry.line_current != null
      ? `${lineText(entry.line_opening)} → ${lineText(entry.line_current)}`
      : "n/a";
  $(".stats", node).append(
    stat("Edge", pts(entry.edge), signClass(entry.edge)),
    stat("EV", `${entry.ev_percent >= 0 ? "+" : ""}${entry.ev_percent.toFixed(1)}%`,
         signClass(entry.ev_percent)),
    stat("Fair odds", american(entry.fair_american_odds)),
    stat("Push", pct(entry.push_probability)),
    stat("Uncertainty", entry.uncertainty.replace("_", " ")),
    stat("Data", entry.data_quality),
    stat("Odds age", `${Math.round(entry.odds_age_minutes)} min`),
    stat("Line move", movement),
    stat("Model", entry.model_version),
  );

  const list = $(".why ul", node);
  for (const check of entry.checks) {
    list.append(el("li", {
      class: check.passed ? "ok" : "fail",
      text: `${check.passed ? "PASS" : "FAIL"} ${check.name}: ${check.actual} (required ${check.required})`,
    }));
  }
  for (const note of entry.notes.filter((n) => !entry.checks.some((c) => n.startsWith(c.name)))) {
    list.append(el("li", { class: "ok", text: note }));
  }

  $(".track", node).addEventListener("click", () => prefillBet(entry));
  $(".add-leg", node).addEventListener("click", () => {
    // A new slip takes the entry's best book; later legs are priced at the slip's book.
    if (!slip.legs.length) setSlipBook(entry.sportsbook);
    addLeg({ game_id: entry.game_id, market: entry.market, selection: entry.selection,
             line: entry.line, label: `${entry.bet} (${entry.matchup})` });
  });
  return node;
}

async function loadCard() {
  const date = $("#card-date").value || todayEastern();
  const status = $("#card-status");
  const refresh = $("#card-refresh");
  refresh.disabled = true;
  status.textContent = "Building the card (the first load fits the model, ~10 s)…";
  try {
    const card = await api(`/api/card?date=${encodeURIComponent(date)}`);
    const header = $("#card-header");
    header.replaceChildren();
    const sports = Object.entries(card.by_sport);
    const withMarket = sports.reduce((n, [, s]) => n + s.with_market, 0);
    const modeled = sports.reduce((n, [, s]) => n + s.modeled, 0);
    header.append(
      el("div", { class: "muted", text: new Date(`${card.date}T12:00:00`).toLocaleDateString(
        "en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" }).toUpperCase() }),
      el("div", {
        class: `headline ${card.headline.startsWith("NO") ? "none" : "some"}`,
        text: card.headline,
      }),
      el("div", { text: `${withMarket} games with a live market · ${modeled} modeled` }),
    );
    if (card.bettable_books == null) {
      header.append(el("div", { class: "warn",
        text: "Best price and EV use every book, including exchanges and prediction markets. Set TTK_BETTABLE_BOOKS." }));
    } else {
      header.append(el("div", { class: "muted", text: `Best price from: ${card.bettable_books.join(", ")}` }));
    }
    if (sports.length) {
      const table = el("table", {}, el("tr", {}, ...["Sport", "Games", "With market", "Modeled",
        "Qualified", "Lean"].map((h) => el("th", { text: h }))));
      for (const [sport, s] of sports) {
        table.append(el("tr", {}, ...[sport, s.games, s.with_market, s.modeled, s.qualified, s.lean]
          .map((v) => el("td", { text: String(v) }))));
      }
      header.append(table);
    }

    const entries = $("#card-entries");
    entries.replaceChildren(...card.entries.map(renderEntry));
    const unmodeled = $("#card-unmodeled");
    unmodeled.replaceChildren();
    if (card.unmodeled.length || card.unbettable.length) {
      const list = el("ul");
      for (const u of card.unmodeled) {
        list.append(el("li", { text: `${u.sport} · ${u.matchup} · ${eastern(u.commence_time)}: ${u.reason}` }));
      }
      for (const side of card.unbettable) {
        list.append(el("li", { text: `${side}: no bettable book quotes this line` }));
      }
      unmodeled.append(el("div", { class: "unmodeled" }, el("h2", { text: "Not evaluated" }), list));
    }
    status.textContent = card.entries.length || card.unmodeled.length
      ? `Updated ${new Date(card.generated_at).toLocaleTimeString()}`
      : "No games with a live market on this date.";
  } catch (error) {
    status.textContent = `Could not load the card: ${error.message}`;
  } finally {
    refresh.disabled = false;
  }
}

$("#card-date").value = todayEastern();
$("#card-date").addEventListener("change", loadCard);
$("#card-refresh").addEventListener("click", loadCard);

// ------------------------------------------------------------------ bet tracker

async function loadBooks() {
  const books = await api("/api/sportsbooks");
  books.sort((a, b) => Number(b.bettable) - Number(a.bettable) || a.key.localeCompare(b.key));
  const options = () =>
    books.map((b) => el("option", { value: b.key, text: b.bettable ? b.key : `${b.key} (not bettable)` }));
  $("#book-select").replaceChildren(...options());
  $("#slip-book").replaceChildren(...options());
  if (slip.book) $("#slip-book").value = slip.book;
  else slip.book = $("#slip-book").value;
}

function prefillBet(entry) {
  const form = $("#bet-form");
  form.game_id.value = entry.game_id;
  form.market.value = entry.market;
  form.selection.value = entry.selection;
  form.line.value = entry.line ?? "";
  form.american_odds.value = Math.round(entry.american_odds);
  form.sportsbook.value = entry.sportsbook;
  form.placed_at.value = "";
  form.notes.value = "";
  $("#bet-form-status").textContent = `Prefilled from the card: ${entry.bet}. Check the odds you actually got.`;
  $("#bet-form-status").className = "muted";
  showTab("bets");
  form.stake.focus();
}

$("#bet-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const status = $("#bet-form-status");
  const body = {
    game_id: Number(form.game_id.value),
    market: form.market.value,
    selection: form.selection.value,
    line: form.line.value === "" ? null : Number(form.line.value),
    american_odds: Number(form.american_odds.value),
    sportsbook: form.sportsbook.value,
    stake: Number(form.stake.value),
    placed_at: form.placed_at.value ? new Date(form.placed_at.value).toISOString() : null,
    notes: form.notes.value || null,
  };
  try {
    const bet = await api("/api/bets", { method: "POST", body: JSON.stringify(body) });
    status.textContent = `Recorded: ${bet.description} ${bet.american_odds}`;
    status.className = "ok-msg";
    form.reset();
    loadBets();
  } catch (error) {
    status.textContent = `Not recorded: ${error.message}`;
    status.className = "err-msg";
  }
});

async function loadBets() {
  const filter = $("#bet-filter").value;
  const bets = await api(`/api/bets${filter ? `?status=${filter}` : ""}`);
  const rows = bets.map((b) => {
    const voidButton = el("button", { class: "secondary", text: "Void" });
    voidButton.hidden = b.result !== "PENDING";
    voidButton.addEventListener("click", async () => {
      if (!confirm(`Void bet ${b.id}: ${b.description}?`)) return;
      await api(`/api/bets/${b.id}`, { method: "PATCH", body: JSON.stringify({ void: true }) });
      loadBets();
    });
    const beliefs = `${pct(b.model_probability)} / ${pct(b.market_probability)}`;
    return el("tr", {},
      el("td", { text: new Date(b.placed_at).toLocaleString() }),
      el("td", { text: b.description }),
      el("td", { text: b.sportsbook ?? "" }),
      el("td", { text: b.american_odds }),
      el("td", { class: "num", text: b.stake?.toFixed(2) ?? "" }),
      el("td", { text: beliefs }),
      el("td", { class: `num ${signClass(b.expected_value)}`, text: signedPct(b.expected_value) }),
      el("td", {}, el("span", { class: `badge ${b.result}`, text: b.result })),
      el("td", { class: `num ${signClass(b.profit_loss)}`, text: b.profit_loss == null ? "" : money(b.profit_loss) }),
      el("td", { class: `num ${signClass(b.clv)}`, text: signedPct(b.clv) }),
      el("td", {}, voidButton),
    );
  });
  const body = $("#bets-table tbody");
  body.replaceChildren(...rows);
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: "11", class: "muted", text: "No bets yet." })));
}

$("#bet-filter").addEventListener("change", loadBets);
$("#bets-settle").addEventListener("click", async () => {
  const status = $("#bets-status");
  try {
    const settled = await api("/api/bets/settle", { method: "POST" });
    status.textContent = `${settled.bets.length} bet(s), ${settled.parlays.length} parlay(s) settled`;
    loadBets();
    loadParlays();
  } catch (error) {
    status.textContent = `Settle failed: ${error.message}`;
  }
});

// ------------------------------------------------------------------ performance

function tile(label, value, sub = "", cls = "") {
  return el("div", { class: "tile" },
    el("div", { class: "label", text: label }),
    el("div", { class: `value ${cls}`, text: value }),
    el("div", { class: "sub", text: sub }));
}

async function loadPerformance() {
  const params = new URLSearchParams();
  if ($("#perf-sport").value) params.set("sport", $("#perf-sport").value);
  if ($("#perf-market").value) params.set("market", $("#perf-market").value);
  const p = await api(`/api/performance${params.size ? `?${params}` : ""}`);
  $("#perf-tiles").replaceChildren(
    tile("Record", `${p.wins}-${p.losses}-${p.pushes}`, `${p.bets} settled · ${p.voids} void · ${p.pending} pending`),
    tile("Hit rate", p.hit_rate == null ? "n/a" : pct(p.hit_rate), "wins / (wins + losses)"),
    tile("Profit", money(p.profit), `staked ${p.staked.toFixed(2)}`, signClass(p.profit)),
    tile("ROI", signedPct(p.roi), `n=${p.bets}`, signClass(p.roi)),
    tile("Units", `${p.units >= 0 ? "+" : ""}${p.units.toFixed(2)}`, `unit = ${p.unit_size}`, signClass(p.units)),
    tile("Avg CLV", signedPct(p.avg_clv), `n=${p.clv_n}`, signClass(p.avg_clv)),
    tile("Avg edge", pts(p.avg_edge), `n=${p.edge_n}`, signClass(p.avg_edge)),
    tile("Avg EV", signedPct(p.avg_ev), `n=${p.ev_n}`, signClass(p.avg_ev)),
    tile("Points vs close", p.avg_points_gained == null ? "n/a" : p.avg_points_gained.toFixed(2),
      `n=${p.points_n}`, signClass(p.avg_points_gained)),
    tile("Max drawdown", p.max_drawdown.toFixed(2), "peak to trough", p.max_drawdown < 0 ? "neg" : ""),
    tile("Open exposure", p.pending_stake.toFixed(2), `${p.pending} pending`),
  );
  const pp = p.parlays;
  $("#perf-parlay-tiles").replaceChildren(
    tile("Record", `${pp.wins}-${pp.losses}-${pp.pushes}`, `${pp.parlays} settled · ${pp.pending} pending`),
    tile("Profit", money(pp.profit), `staked ${pp.staked.toFixed(2)}`, signClass(pp.profit)),
    tile("ROI", signedPct(pp.roi), `n=${pp.parlays}`, signClass(pp.roi)),
    tile("Avg EV at placement", signedPct(pp.avg_ev), `n=${pp.ev_n}`, signClass(pp.avg_ev)),
  );
}

$("#perf-sport").addEventListener("change", loadPerformance);
$("#perf-market").addEventListener("change", loadPerformance);

// ------------------------------------------------------------------ parlay lab

const SLIP_KEY = "ttk-slip";
const slip = (() => {
  try {
    const saved = JSON.parse(localStorage.getItem(SLIP_KEY) || "null");
    if (saved && Array.isArray(saved.legs)) return saved;
  } catch { /* storage unavailable: start empty */ }
  return { book: null, legs: [] };
})();

function persistSlip() {
  try { localStorage.setItem(SLIP_KEY, JSON.stringify(slip)); } catch { /* ignore */ }
  $("#slip-count").textContent = slip.legs.length ? String(slip.legs.length) : "";
}

function setSlipBook(book) {
  slip.book = book;
  $("#slip-book").value = book;
  persistSlip();
  analyze();
}

function addLeg(leg) {
  const same = (a) => a.game_id === leg.game_id && a.market === leg.market &&
    a.selection === leg.selection && a.line === leg.line;
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
  const list = $("#slip-legs");
  list.replaceChildren(...slip.legs.map((leg, i) => {
    const remove = el("button", { class: "secondary", text: "Remove", "aria-label": `Remove leg ${i + 1}` });
    remove.addEventListener("click", () => removeLeg(i));
    const odds = el("input", { type: "number", class: "leg-odds", placeholder: "book price",
      "aria-label": `Odds you were offered for leg ${i + 1} (optional)`,
      value: leg.american_odds ?? "" });
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
  const clear = () => {
    $("#lab-summary").replaceChildren();
    $("#lab-warnings").replaceChildren();
    $("#lab-legs tbody").replaceChildren();
  };
  if (slip.legs.length < 2 || !slip.book) {
    clear();
    status.textContent = "Add at least two legs.";
    return;
  }
  status.textContent = "Analyzing…";
  try {
    const body = { sportsbook: slip.book, legs: slip.legs.map(({ label, ...leg }) => leg) };
    const a = await api("/api/parlays/evaluate", { method: "POST", body: JSON.stringify(body) });
    if (seq !== analyzeSeq) return;  // a newer slip superseded this one
    status.textContent = `${a.legs.length} legs at ${a.sportsbook}`;
    $("#lab-summary").replaceChildren(
      tile("Parlay odds", american(a.american_odds), `book implied ${pct(a.book_implied_probability)}`),
      tile("Joint probability", pct(a.joint_probability, 2),
        a.correlations.length ? "assumes independence" : "independent legs"),
      tile("Fair odds", american(a.fair_american_odds),
        a.market_joint_probability == null ? "" : `market joint ${pct(a.market_joint_probability, 2)}`),
      tile("EV", signedPct(a.ev_per_unit), "per unit staked", signClass(a.ev_per_unit)),
      tile("Correlation", a.correlation_risk, `${a.correlations.length} same-game pair(s)`,
        a.correlation_risk === "LOW" ? "" : "neg"),
    );
    $("#lab-warnings").replaceChildren(
      ...a.warnings.map((w) => el("li", { text: w })),
      ...a.correlations.map((c) => el("li", { text: `Legs ${c.legs[0] + 1} & ${c.legs[1] + 1}: ${c.risk} - ${c.reason}` })),
    );
    const impact = Object.fromEntries(a.impacts.map((i) => [i.index, i.ev_without]));
    $("#lab-legs tbody").replaceChildren(...a.legs.map((leg) => {
      const tags = el("div");
      const tag = (text, cls = "") => tags.append(el("span", { class: `tag ${cls}`, text }));
      if (leg.index === a.strongest_leg) tag("strongest", "good");
      if (leg.index === a.weakest_leg) tag("weakest", "bad");
      if (leg.index === a.lowest_edge_leg && leg.edge != null) tag("lowest edge", "bad");
      if (leg.index === a.highest_correlation_leg) tag("most correlated", "bad");
      if (leg.index === a.reduces_ev_most) tag(`costs most EV (without: ${signedPct(impact[leg.index])})`, "bad");
      const remove = el("button", { class: "secondary", text: "Remove" });
      remove.addEventListener("click", () => removeLeg(leg.index));
      const age = leg.odds_age_minutes == null ? leg.price_source : `${Math.round(leg.odds_age_minutes)} min`;
      return el("tr", {},
        el("td", {}, el("div", { text: leg.description }),
          el("div", { class: "muted", text: `${leg.sport} · ${leg.matchup} · ${eastern(leg.commence_time)}` }), tags),
        el("td", { text: `${american(leg.american_odds)} (${age})` }),
        el("td", { text: pct(leg.probability) }),
        el("td", { text: leg.probability_source }),
        el("td", { class: `num ${signClass(leg.edge)}`, text: leg.edge == null ? "n/a" : pts(leg.edge) }),
        el("td", { class: `num ${signClass(leg.ev_per_unit)}`, text: signedPct(leg.ev_per_unit) }),
        el("td", { text: leg.correlation_risk }),
        el("td", {}, remove),
      );
    }));
  } catch (error) {
    if (seq !== analyzeSeq) return;
    clear();
    status.textContent = error.message;
  }
}

$("#slip-book").addEventListener("change", (event) => setSlipBook(event.target.value));
$("#slip-clear").addEventListener("click", () => {
  if (slip.legs.length && !confirm("Clear every leg from the slip?")) return;
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
  if (seq !== pickSeq) return;  // a newer date/sport choice superseded this one
  $("#pick-game").replaceChildren(
    el("option", { value: "", text: games.length ? "Choose a game" : "No upcoming games" }),
    ...games.map((g) => el("option", { value: g.id, text: `${g.sport} · ${g.away_team} @ ${g.home_team} · ${eastern(g.commence_time)}` })),
  );
  $("#pick-offers").replaceChildren();
}

let offersSeq = 0;
async function loadOffers() {
  const seq = ++offersSeq;
  const gameId = Number($("#pick-game").value);
  const box = $("#pick-offers");
  box.replaceChildren();
  if (!gameId || !slip.book) return;
  const option = $("#pick-game").selectedOptions[0];
  const matchup = option ? option.textContent.split(" · ")[1] : "";
  const [away, home] = matchup.split(" @ ");
  const offers = await api(`/api/games/${gameId}/offers?book=${encodeURIComponent(slip.book)}`);
  if (seq !== offersSeq) return;
  if (!offers.length) {
    box.append(el("p", { class: "muted", text: `${slip.book} has no lines for this game.` }));
    return;
  }
  let group = "";
  // Group by market; within a market, the book's main lines first.
  const sorted = [...offers].sort(
    (a, b) => a.market.localeCompare(b.market) || Number(b.main) - Number(a.main),
  );
  for (const o of sorted) {
    if (o.market !== group) {
      group = o.market;
      box.append(el("div", { class: "group", text: group }));
    }
    const side = o.market === "TOTAL"
      ? o.selection.charAt(0) + o.selection.slice(1).toLowerCase()
      : (o.selection === "HOME" ? home : away);
    const text = o.market === "MONEYLINE" ? `${side} ML` : o.market === "TOTAL"
      ? `${side} ${o.line}` : `${side} ${lineText(o.line)}`;
    const button = el("button", { class: `secondary ${o.main ? "main" : ""}`,
      text: `${text}  ${american(o.american_odds)}${o.main ? " · main" : ""}` });
    button.addEventListener("click", () =>
      addLeg({ game_id: gameId, market: o.market, selection: o.selection, line: o.line,
               label: `${text} (${matchup})` }));
    box.append(button);
  }
}

$("#pick-date").value = todayEastern();
$("#pick-date").addEventListener("change", loadPickGames);
$("#pick-sport").addEventListener("change", loadPickGames);
$("#pick-game").addEventListener("change", loadOffers);

$("#parlay-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const status = $("#parlay-form-status");
  const body = {
    sportsbook: slip.book,
    legs: slip.legs.map(({ label, ...leg }) => leg),
    stake: Number(form.stake.value),
    american_odds: form.american_odds.value === "" ? null : Number(form.american_odds.value),
    placed_at: form.placed_at.value ? new Date(form.placed_at.value).toISOString() : null,
    notes: form.notes.value || null,
  };
  try {
    const saved = await api("/api/parlays", { method: "POST", body: JSON.stringify(body) });
    status.textContent = `Recorded parlay ${saved.id} at ${saved.american_odds}`;
    status.className = "ok-msg";
    form.reset();
  } catch (error) {
    status.textContent = `Not recorded: ${error.message}`;
    status.className = "err-msg";
  }
});

async function loadParlays() {
  const parlays = await api("/api/parlays");
  const body = $("#parlays-table tbody");
  body.replaceChildren(...parlays.map((p) => el("tr", {},
    el("td", { text: p.placed_at ? new Date(p.placed_at).toLocaleString() : "" }),
    el("td", {}, ...p.legs.map((leg) => el("div", {
      text: `${leg.result === "PENDING" ? "" : `[${leg.result}] `}${leg.description} ${leg.american_odds}` }))),
    el("td", { text: p.sportsbook ?? "" }),
    el("td", { text: p.american_odds ?? "" }),
    el("td", { class: "num", text: p.stake?.toFixed(2) ?? "" }),
    el("td", { class: "num", text: pct(p.joint_probability, 2) }),
    el("td", { class: `num ${signClass(p.expected_value)}`, text: signedPct(p.expected_value) }),
    el("td", { text: p.correlation_risk ?? "" }),
    el("td", {}, el("span", { class: `badge ${p.result}`, text: p.result })),
    el("td", { class: `num ${signClass(p.profit_loss)}`, text: p.profit_loss == null ? "" : money(p.profit_loss) }),
  )));
  if (!parlays.length) body.append(el("tr", {}, el("td", { colspan: "10", class: "muted", text: "No parlays yet." })));
}

// ------------------------------------------------------------------ start

persistSlip();

loadBooks().catch(() => {});
loadCard();
