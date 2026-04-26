const state = {
  dashboard: null,
  hotDisplayLimits: {
    pokemon: 10,
    ws: 10,
  },
};

async function loadDashboard() {
  const response = await fetch("/api/dashboard", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Dashboard request failed: ${response.status}`);
  }

  state.dashboard = await response.json();
  renderDashboard(state.dashboard);
  await loadHotBoards();
}

async function loadHotBoards() {
  renderHotBoardsLoading();
  const response = await fetch("/api/hot-cards", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Hot board request failed: ${response.status}`);
  }
  const payload = await response.json();
  if (state.dashboard) {
    state.dashboard.hot_cards = payload.hot_cards;
    state.dashboard.hot_cards_error = payload.hot_cards_error;
  }
  renderHotBoards(payload.hot_cards, payload.hot_cards_error);
}

function renderDashboard(payload) {
  renderAssistantMeta(payload.assistant);
  renderStats(payload.stats);
  renderTools(payload.tools);
  renderSources(payload.reference_sources);
  renderWatchlist(payload.example_watchlist);
  renderTrackedItems(payload.tracked_items);
}

function renderHotBoardsLoading() {
  for (const game of ["pokemon", "ws"]) {
    const methodology = document.getElementById(`hot-${game}-methodology`);
    const summary = document.getElementById(`hot-${game}-summary`);
    const list = document.getElementById(`hot-${game}-list`);
    const limitSelect = document.getElementById(`hot-${game}-limit`);
    methodology.textContent = "正在載入高流動性資料…";
    summary.textContent = "";
    list.innerHTML = `<div class="hot-item empty-state">正在整理最新的高流動性資料，請稍候。</div>`;
    configureHotBoardLimit(limitSelect, [], 0, game);
  }
}

function renderStats(stats) {
  document.getElementById("stat-tracked-items").textContent = stats.tracked_items;
  document.getElementById("stat-watch-rules").textContent = stats.watch_rules;
  document.getElementById("stat-source-offers").textContent = stats.source_offers;
  document.getElementById("stat-price-snapshots").textContent = stats.price_snapshots;
}

function renderAssistantMeta(assistant) {
  const root = document.getElementById("assistant-meta");
  root.innerHTML = "";
  const entries = [
    ["Name", assistant.name],
    ["Environment", assistant.environment],
    ["Log Level", assistant.log_level],
    ["DB Path", assistant.monitor_db_path],
    ["Telegram", assistant.telegram_configured ? "Configured" : "Not configured"],
  ];

  for (const [label, value] of entries) {
    const card = document.createElement("div");
    card.className = "meta-card";
    card.innerHTML = `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd>`;
    root.appendChild(card);
  }
}

function renderTools(tools) {
  const root = document.getElementById("tools-list");
  root.innerHTML = "";

  for (const tool of tools) {
    const aliases = tool.aliases.length
      ? `<div class="tag-row">${tool.aliases.map((alias) => `<span class="alias">${escapeHtml(alias)}</span>`).join("")}</div>`
      : `<p class="muted">No aliases</p>`;
    const card = document.createElement("article");
    card.className = "tool-card";
    card.innerHTML = `
      <h3>${escapeHtml(tool.name)}</h3>
      <p class="source-meta">${escapeHtml(tool.description)}</p>
      ${aliases}
    `;
    root.appendChild(card);
  }
}

function renderSources(sources) {
  const root = document.getElementById("source-list");
  root.innerHTML = "";

  for (const source of sources) {
    const card = document.createElement("article");
    card.className = "source-card";
    card.innerHTML = `
      <div class="tag-row">
        <span class="tag">${escapeHtml(source.source_kind)}</span>
        ${source.games.map((game) => `<span class="tag">${escapeHtml(game)}</span>`).join("")}
      </div>
      <h3>${escapeHtml(source.name)}</h3>
      <p class="source-meta">trust ${formatDecimal(source.trust_score)} | weight ${formatDecimal(source.price_weight)}</p>
      <div class="tag-row">
        ${source.reference_roles.map((role) => `<span class="alias">${escapeHtml(role)}</span>`).join("")}
      </div>
      <p class="muted">${escapeHtml(source.notes)}</p>
      <a class="source-link" href="${escapeHtml(source.url)}" target="_blank" rel="noreferrer">Open source page</a>
    `;
    root.appendChild(card);
  }
}

function renderWatchlist(items) {
  const root = document.getElementById("watchlist-list");
  root.innerHTML = "";

  for (const item of items) {
    const card = document.createElement("article");
    card.className = "watch-card";
    card.innerHTML = `
      <div class="tag-row">
        <span class="tag">${escapeHtml(item.game)}</span>
        <span class="alias">${escapeHtml(item.rarity || "n/a")}</span>
      </div>
      <h3>${escapeHtml(item.title)}</h3>
      <p class="source-meta">${escapeHtml(item.card_number || "no card number")} | ${escapeHtml(item.set_name || "no set name")}</p>
      <p class="muted">set code: ${escapeHtml(item.set_code || "n/a")}</p>
    `;
    root.appendChild(card);
  }
}

function renderTrackedItems(items) {
  const root = document.getElementById("tracked-items");
  root.innerHTML = "";

  if (!items.length) {
    root.innerHTML = `<div class="tracked-card empty-state">目前資料庫還沒有 tracked items。可以先執行 seed-example-watchlist 或從這個頁面直接查卡價。</div>`;
    return;
  }

  for (const item of items) {
    const attributes = item.attributes || {};
    const chips = [
      attributes.game,
      attributes.card_number,
      attributes.rarity,
      item.enabled === null ? null : item.enabled ? "enabled" : "disabled",
    ].filter(Boolean);
    const fairValue = item.fair_value_jpy == null ? "n/a" : `¥${Number(item.fair_value_jpy).toLocaleString()}`;
    const card = document.createElement("article");
    card.className = "tracked-card";
    card.innerHTML = `
      <div class="tag-row">
        ${chips.map((chip) => `<span class="tag">${escapeHtml(String(chip))}</span>`).join("")}
      </div>
      <h3>${escapeHtml(item.title)}</h3>
      <p class="source-meta">fair value ${escapeHtml(fairValue)} | confidence ${escapeHtml(item.confidence == null ? "n/a" : formatDecimal(item.confidence))}</p>
      <p class="muted">schedule ${escapeHtml(item.schedule_minutes == null ? "n/a" : `${item.schedule_minutes} min`)} | threshold ${escapeHtml(item.discount_threshold_pct == null ? "n/a" : `${item.discount_threshold_pct}%`)}</p>
    `;
    root.appendChild(card);
  }
}

function renderHotBoards(boards, errorMessage) {
  const safeBoards = Array.isArray(boards) ? boards : [];
  const pokemonBoard = safeBoards.find((board) => board.game === "pokemon");
  const wsBoard = safeBoards.find((board) => board.game === "ws");

  renderHotBoard("pokemon", pokemonBoard, errorMessage);
  renderHotBoard("ws", wsBoard, errorMessage);
}

function renderHotBoard(game, board, errorMessage) {
  const methodology = document.getElementById(`hot-${game}-methodology`);
  const summary = document.getElementById(`hot-${game}-summary`);
  const list = document.getElementById(`hot-${game}-list`);
  const limitSelect = document.getElementById(`hot-${game}-limit`);

  if (!board) {
    methodology.textContent = errorMessage || "流動性榜目前無法載入。";
    summary.textContent = "";
    list.innerHTML = `<div class="hot-item empty-state">目前沒有可顯示的高流動性資料。</div>`;
    configureHotBoardLimit(limitSelect, [], 0, game);
    return;
  }

  const items = Array.isArray(board.items) ? board.items : [];
  const allowedLimits = Array.isArray(board.allowed_display_limits) ? board.allowed_display_limits : [];
  const defaultLimit = Number(board.default_display_limit || 0);
  configureHotBoardLimit(limitSelect, allowedLimits, defaultLimit, game);

  methodology.textContent = board.methodology;

  if (!items.length) {
    summary.textContent = "目前來源有回應，但沒有可顯示的高流動性項目。";
    list.innerHTML = `<div class="hot-item empty-state">目前來源有回應，但沒有可顯示的高流動性項目。</div>`;
    return;
  }

  const selectedLimit = Math.min(state.hotDisplayLimits[game] || defaultLimit || items.length, items.length);
  const generatedAt = formatDateTime(board.generated_at);
  summary.textContent = `目前顯示前 ${selectedLimit} / ${items.length} 名。資料更新時間 ${generatedAt}。`;

  list.innerHTML = "";

  for (const item of items.slice(0, selectedLimit)) {
    const cardInfo = [item.card_number, item.rarity, item.set_code].filter(Boolean);
    const references = Array.isArray(item.references) ? item.references : [];
    const notesList = Array.isArray(item.notes) ? item.notes : [];
    const links = references
      .map(
        (reference) =>
          `<a class="source-link" href="${escapeHtml(reference.url)}" target="_blank" rel="noreferrer">${escapeHtml(reference.label)}</a>`,
      )
      .join("");
    const notes = notesList.map((note) => `<div class="hot-note">${escapeHtml(note)}</div>`).join("");
    const priceLabel = item.price_jpy == null ? "price n/a" : `¥${Number(item.price_jpy).toLocaleString()}`;
    const thumbnailMarkup = item.thumbnail_url
      ? `
          <a class="hot-thumb" href="${escapeHtml(firstReferenceUrl(references) || "#")}" target="_blank" rel="noreferrer">
            <img class="hot-thumb__image" src="${escapeHtml(item.thumbnail_url)}" alt="${escapeHtml(item.title)}" loading="lazy" />
            <span class="hot-thumb__preview" aria-hidden="true">
              <img class="hot-thumb__preview-image" src="${escapeHtml(item.thumbnail_url)}" alt="" loading="lazy" />
            </span>
          </a>
        `
      : `<div class="hot-thumb hot-thumb--empty">No image</div>`;

    const article = document.createElement("article");
    article.className = "hot-item";
    article.innerHTML = `
      ${thumbnailMarkup}
      <div class="hot-item__topline">
        <span class="hot-rank">#${escapeHtml(item.rank)}</span>
        <div class="hot-price">${escapeHtml(priceLabel)}</div>
      </div>
      <h3 class="hot-item__title">${escapeHtml(item.title)}</h3>
      <div class="hot-meta">
        ${cardInfo.map((value) => `<span class="tag">${escapeHtml(value)}</span>`).join("")}
        ${item.best_bid_jpy == null ? "" : `<span class="alias">bid ¥${escapeHtml(Number(item.best_bid_jpy).toLocaleString())}</span>`}
        ${item.buy_signal_label === "priceup" ? `<span class="alias">buy up</span>` : ""}
        ${item.best_ask_jpy == null ? "" : `<span class="alias">ask ¥${escapeHtml(Number(item.best_ask_jpy).toLocaleString())}</span>`}
        ${item.bid_ask_ratio == null ? "" : `<span class="alias">bid/ask ${escapeHtml(formatPercent(item.bid_ask_ratio))}</span>`}
        ${item.buy_support_score == null ? "" : `<span class="alias">support ${escapeHtml(formatDecimal(item.buy_support_score))}</span>`}
        ${item.momentum_boost_score == null || item.momentum_boost_score <= 0 ? "" : `<span class="alias">boost ${escapeHtml(formatDecimal(item.momentum_boost_score))}</span>`}
        ${item.liquidity_score == null ? "" : `<span class="alias">liq ${escapeHtml(formatDecimal(item.liquidity_score))}</span>`}
        ${item.attention_score == null ? "" : `<span class="alias">attn ${escapeHtml(formatDecimal(item.attention_score))}</span>`}
        ${item.social_post_count == null ? "" : `<span class="alias">sns ${escapeHtml(item.social_post_count)} posts</span>`}
        ${item.is_graded ? `<span class="alias">graded</span>` : ""}
      </div>
      <div class="hot-notes">${notes}</div>
      <div class="hot-links">${links}</div>
    `;
    list.appendChild(article);
  }
}

function configureHotBoardLimit(select, allowedLimits, defaultLimit, game) {
  select.innerHTML = "";

  if (!allowedLimits.length) {
    select.disabled = true;
    const option = document.createElement("option");
    option.value = "0";
    option.textContent = "0";
    select.appendChild(option);
    state.hotDisplayLimits[game] = 0;
    return;
  }

  const nextValue = allowedLimits.includes(state.hotDisplayLimits[game])
    ? state.hotDisplayLimits[game]
    : defaultLimit || allowedLimits[allowedLimits.length - 1];
  state.hotDisplayLimits[game] = nextValue;

  for (const limit of allowedLimits) {
    const option = document.createElement("option");
    option.value = String(limit);
    option.textContent = String(limit);
    option.selected = limit === nextValue;
    select.appendChild(option);
  }
  select.disabled = false;
}

async function submitLookup(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const params = new URLSearchParams();
  for (const [key, value] of data.entries()) {
    const trimmed = String(value).trim();
    if (trimmed) {
      params.set(key, trimmed);
    }
  }

  const status = document.getElementById("lookup-status");
  const result = document.getElementById("lookup-result");
  status.textContent = "查詢中";
  result.classList.remove("empty-state");
  result.innerHTML = "<pre>Working...</pre>";

  try {
    const response = await fetch(`/api/tcg/lookup?${params.toString()}`);
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.details || payload.error || `Lookup failed: ${response.status}`);
    }

    status.textContent = "查詢完成";
    result.innerHTML = `<pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;
    await loadDashboard();
  } catch (error) {
    status.textContent = "查詢失敗";
    result.innerHTML = `<pre>${escapeHtml(error.message)}</pre>`;
  }
}

function handleHotLimitChange(event) {
  const select = event.currentTarget;
  const game = select.dataset.game;
  state.hotDisplayLimits[game] = Number(select.value);
  if (state.dashboard) {
    renderHotBoards(state.dashboard.hot_cards, state.dashboard.hot_cards_error);
  }
}

function firstReferenceUrl(references) {
  if (!Array.isArray(references) || !references.length) {
    return null;
  }
  return references[references.length - 1]?.url || references[0]?.url || null;
}

function formatDateTime(value) {
  if (!value) {
    return "n/a";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "n/a";
  }
  return new Intl.DateTimeFormat("zh-Hant-TW", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatPercent(value) {
  return `${(Number(value) * 100).toFixed(0)}%`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatDecimal(value) {
  return Number(value).toFixed(2);
}

document.getElementById("lookup-form").addEventListener("submit", submitLookup);
document.getElementById("refresh-dashboard").addEventListener("click", () => {
  loadDashboard().catch(renderFatalError);
  loadMercariWatchlist().catch(console.error);
});
document.getElementById("hot-pokemon-limit").addEventListener("change", handleHotLimitChange);
document.getElementById("hot-ws-limit").addEventListener("change", handleHotLimitChange);
document.getElementById("mercari-watch-form").addEventListener("submit", submitMercariWatch);

loadDashboard().catch(renderFatalError);
loadMercariWatchlist().catch(console.error);

// ─── Mercari Watchlist ───────────────────────────────────────────────────────

async function loadMercariWatchlist() {
  const response = await fetch("/api/mercari-watchlist", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Watchlist request failed: ${response.status}`);
  }
  const watches = await response.json();
  renderMercariWatchlist(watches);
}

function renderMercariWatchlist(watches) {
  const root = document.getElementById("mercari-watch-list");
  root.innerHTML = "";

  if (!watches.length) {
    root.innerHTML = `<div class="tracked-card empty-state">目前沒有追蹤項目。在上方表單新增。</div>`;
    return;
  }

  for (const watch of watches) {
    const card = document.createElement("article");
    card.className = "mercari-watch-card";
    card.dataset.watchId = watch.watch_id;

    const statusClass = watch.enabled ? "watch-status--on" : "watch-status--off";
    const statusLabel = watch.enabled ? "啟用中" : "已停用";
    const checked = watch.last_checked_at
      ? watch.last_checked_at.replace("T", " ").slice(0, 16)
      : "尚未檢查";

    const hitsHtml = Array.isArray(watch.recent_hits) && watch.recent_hits.length
      ? watch.recent_hits.map(h => `
          <div class="watch-hit">
            <a class="watch-hit__link" href="${escapeHtml(h.url)}" target="_blank" rel="noreferrer">
              ${h.thumbnail_url ? `<img class="watch-hit__thumb" src="${escapeHtml(h.thumbnail_url)}" alt="" loading="lazy" />` : ""}
              <span class="watch-hit__title">${escapeHtml(h.title || "（無標題）")}</span>
            </a>
            <span class="watch-hit__price">¥${Number(h.price_jpy).toLocaleString()}</span>
            <span class="muted">${escapeHtml(h.first_seen_at.replace("T", " ").slice(0, 16))}</span>
          </div>
        `).join("")
      : `<p class="muted">尚無命中紀錄。</p>`;

    card.innerHTML = `
      <div class="watch-card__header">
        <div>
          <span class="watch-status ${statusClass}">${escapeHtml(statusLabel)}</span>
          <span class="tag" style="font-size:0.72rem;margin-left:4px">${escapeHtml(watch.watch_id)}</span>
        </div>
        <div class="watch-card__actions">
          <button class="button button--small button--ghost" data-action="toggle" title="${watch.enabled ? "停用" : "啟用"}">
            ${watch.enabled ? "停用" : "啟用"}
          </button>
          <button class="button button--small button--danger" data-action="delete" title="刪除">刪除</button>
        </div>
      </div>
      <h3 class="watch-card__query">${escapeHtml(watch.query)}</h3>
      <p class="source-meta">價格上限：¥${Number(watch.price_threshold_jpy).toLocaleString()} | 最後檢查：${escapeHtml(checked)}</p>
      <details class="watch-hits">
        <summary>近期命中（${watch.recent_hits ? watch.recent_hits.length : 0} 筆）</summary>
        <div class="watch-hits__list">${hitsHtml}</div>
      </details>
    `;

    card.querySelector("[data-action='toggle']").addEventListener("click", () => {
      toggleMercariWatch(watch.watch_id, !watch.enabled);
    });
    card.querySelector("[data-action='delete']").addEventListener("click", () => {
      deleteMercariWatch(watch.watch_id);
    });

    root.appendChild(card);
  }
}

async function submitMercariWatch(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const query = String(data.get("query") || "").trim();
  const threshold = parseInt(String(data.get("price_threshold_jpy") || "0"), 10);
  const chatId = String(data.get("chat_id") || "").trim() || "dashboard";

  const status = document.getElementById("mercari-watch-status");
  status.style.display = "";
  status.textContent = "新增中…";

  try {
    const response = await fetch("/api/mercari-watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, price_threshold_jpy: threshold, chat_id: chatId }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    status.textContent = `已新增追蹤 [${payload.watch_id}]`;
    form.reset();
    await loadMercariWatchlist();
  } catch (error) {
    status.textContent = `新增失敗：${error.message}`;
  }
}

async function deleteMercariWatch(watchId) {
  if (!confirm(`確定要刪除追蹤 [${watchId}]？`)) return;
  try {
    const response = await fetch(`/api/mercari-watchlist/${encodeURIComponent(watchId)}`, {
      method: "DELETE",
    });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    await loadMercariWatchlist();
  } catch (error) {
    alert(`刪除失敗：${error.message}`);
  }
}

async function toggleMercariWatch(watchId, enabled) {
  try {
    const response = await fetch(`/api/mercari-watchlist/${encodeURIComponent(watchId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    await loadMercariWatchlist();
  } catch (error) {
    alert(`更新失敗：${error.message}`);
  }
}

// ─── Fatal Error ─────────────────────────────────────────────────────────────

function renderFatalError(error) {
  const result = document.getElementById("lookup-result");
  result.classList.remove("empty-state");
  result.innerHTML = `<pre>${escapeHtml(error.message)}</pre>`;
  document.getElementById("lookup-status").textContent = "載入失敗";
  renderHotBoards([], `載入失敗: ${error.message}`);
}
