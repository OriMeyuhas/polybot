const API_BASE = window.location.origin;
const WS_URL = `ws://${window.location.host}/ws`;
let ws = null;
let wsReconnectTimer = null;
let pollTimer = null;
let agentRunning = false;
let hadConnected = false;
let prevTradeCount = 0;
let prevTotalPnl = null;
let _openDetailSlug = null;

// PnL sparkline history
const PNL_HISTORY_MAX = 60;
const pnlHistory = [];

// ────── Toast System ──────
function showToast(message, type = "info") {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add("removing");
    toast.addEventListener("animationend", () => toast.remove());
  }, 4000);
}

// ────── Modal System ──────
let modalResolve = null;

function showModal(message) {
  return new Promise((resolve) => {
    modalResolve = resolve;
    document.getElementById("modal-body").textContent = message;
    document.getElementById("modal-overlay").style.display = "";
  });
}

document.getElementById("modal-confirm").addEventListener("click", () => {
  document.getElementById("modal-overlay").style.display = "none";
  if (modalResolve) modalResolve(true);
  modalResolve = null;
});

document.getElementById("modal-cancel").addEventListener("click", () => {
  document.getElementById("modal-overlay").style.display = "none";
  if (modalResolve) modalResolve(false);
  modalResolve = null;
});

// ────── Start agent ──────
document.getElementById("btn-start").addEventListener("click", async () => {
  try {
    const resp = await fetch(`${API_BASE}/api/start`, { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
      agentRunning = true;
      hadConnected = false;
      pnlHistory.length = 0;
      document.getElementById("btn-start").disabled = true;
      document.getElementById("btn-stop").disabled = false;
      connectWebSocket();
      startPolling();
      showToast("Bot started.", "success");
    } else {
      showToast(data.error || "Start failed", "error");
    }
  } catch (e) {
    const msg = e.message && e.message.includes("fetch")
      ? "Cannot reach server. Is it running?"
      : String(e);
    showToast("Start failed: " + msg, "error");
  }
});

// ────── Stop agent ──────
document.getElementById("btn-stop").addEventListener("click", async () => {
  try {
    const resp = await fetch(`${API_BASE}/api/stop`, { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
      agentRunning = false;
      hadConnected = false;
      stopPolling();
      disconnectWebSocket();
      document.getElementById("btn-stop").disabled = true;
      document.getElementById("btn-start").disabled = false;
      updateConnectionStatus(false);
      showToast("Bot stopped.", "info");
    }
  } catch (e) {
    console.error("Stop failed:", e);
  }
});

// ────── WebSocket ──────
function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  try {
    ws = new WebSocket(WS_URL);
    ws.onopen = () => {
      stopPolling();
    };
    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        applyStatus(data);
      } catch (err) {
        console.error("WS parse error:", err);
      }
    };
    ws.onclose = () => {
      ws = null;
      if (agentRunning) {
        startPolling();
        wsReconnectTimer = setTimeout(connectWebSocket, 3000);
      }
    };
    ws.onerror = () => {
      ws?.close();
    };
  } catch (e) {
    if (agentRunning) {
      startPolling();
      wsReconnectTimer = setTimeout(connectWebSocket, 3000);
    }
  }
}

function disconnectWebSocket() {
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = null;
  }
  if (ws) {
    ws.onclose = null;
    ws.close();
    ws = null;
  }
}

function updateConnectionStatus(connected, marketName) {
  const dot = document.getElementById("live-dot");
  const text = document.getElementById("status-text");
  dot.classList.toggle("connected", connected);
  dot.classList.toggle("disconnected", !connected && agentRunning);
  dot.classList.toggle("error", !connected && agentRunning && typeof marketName === "string" && marketName.startsWith("ERROR:"));
  if (connected) {
    text.textContent = "Live";
  } else if (agentRunning && typeof marketName === "string" && (marketName.startsWith("ERROR:") || marketName.endsWith("..."))) {
    text.textContent = marketName;
  } else {
    text.textContent = agentRunning ? "Connecting..." : "Offline";
  }
}

function syncButtons(connected) {
  document.getElementById("btn-start").disabled = agentRunning;
  document.getElementById("btn-stop").disabled = !agentRunning;
}

function onAgentStarted() {
  if (agentRunning) return;
  agentRunning = true;
  hadConnected = true;
  connectWebSocket();
  startPolling();
}

function onAgentStopped() {
  agentRunning = false;
  hadConnected = false;
  stopPolling();
  disconnectWebSocket();
}

// ────── Polling (fallback when WS disconnects) ──────
function startPolling() {
  if (pollTimer) return;
  const poll = async () => {
    if (!agentRunning) return;
    if (ws && ws.readyState === WebSocket.OPEN) {
      stopPolling();
      return;
    }
    try {
      const resp = await fetch(`${API_BASE}/api/state`);
      const data = await resp.json();
      applyStatus(data);
    } catch (e) {
      console.error("Poll failed:", e);
    }
    if (agentRunning) pollTimer = setTimeout(poll, 2000);
  };
  pollTimer = setTimeout(poll, 500);
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

// ────── Helpers ──────
function fmtUsd(val) {
  const n = Number(val) || 0;
  const sign = n > 0 ? "+" : n < 0 ? "\u2212" : "";
  return sign + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtPrice(val) {
  if (val == null) return "\u2014";
  const n = Number(val);
  const decimals = n < 10 ? 4 : 2;
  return "$" + n.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtRuntime(sec) {
  if (sec == null || sec <= 0) return "0s";
  const s = Math.floor(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return `${m}m ${rem}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function fmtPct(val) {
  if (val == null) return "--";
  return (Number(val) * 100).toFixed(1) + "%";
}

function flashEl(el) {
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

// ────── Sparkline Renderer ──────
function renderSparkline(history) {
  const line = document.getElementById("spark-line");
  const fill = document.getElementById("spark-fill");
  const gradStop = document.getElementById("spark-grad-stop");
  if (!history.length) {
    line.setAttribute("points", "");
    fill.setAttribute("d", "");
    return;
  }

  const W = 240, H = 48, pad = 2;
  const values = history.slice(-PNL_HISTORY_MAX);
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 0);
  const range = max - min || 1;

  const points = values.map((v, i) => {
    const x = (i / Math.max(values.length - 1, 1)) * W;
    const y = pad + (H - 2 * pad) * (1 - (v - min) / range);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  line.setAttribute("points", points.join(" "));

  const lastVal = values[values.length - 1];
  const color = lastVal >= 0 ? "var(--accent-green)" : "var(--accent-red)";
  const gradColor = lastVal >= 0 ? "rgba(0,214,143,0.25)" : "rgba(255,77,106,0.25)";
  line.setAttribute("stroke", color);
  gradStop.setAttribute("stop-color", gradColor);

  const pathD = `M${points[0]} ` + points.slice(1).map(p => `L${p}`).join(" ") +
    ` L${W},${H} L0,${H} Z`;
  fill.setAttribute("d", pathD);
}

// ────── Price Strip Renderer ──────
function renderPriceStrip(prices, binancePrices) {
  const strip = document.getElementById("price-strip");
  const header = document.getElementById("price-strip-header");
  if (!prices || Object.keys(prices).length === 0) {
    strip.style.display = "none";
    if (header) header.style.display = "none";
  } else {
    strip.style.display = "flex";
    if (header) header.style.display = "";
    for (const sym of ["BTC", "ETH", "SOL", "XRP"]) {
      const el = document.getElementById(`strip-price-${sym}`);
      if (el && prices[sym] != null) {
        el.textContent = fmtPrice(prices[sym]);
      }
    }
  }

  const bStrip = document.getElementById("binance-strip");
  const bHeader = document.getElementById("binance-strip-header");
  if (!binancePrices || Object.keys(binancePrices).length === 0) {
    if (bStrip) bStrip.style.display = "none";
    if (bHeader) bHeader.style.display = "none";
  } else {
    if (bStrip) bStrip.style.display = "flex";
    if (bHeader) bHeader.style.display = "";
    for (const sym of ["BTC", "ETH", "SOL", "XRP"]) {
      const el = document.getElementById(`binance-price-${sym}`);
      if (el && binancePrices[sym] != null) {
        el.textContent = fmtPrice(binancePrices[sym]);
      }
    }
  }
}

let _lastMarkets = [];
let _lastActivityFeed = [];

function _bookMid(bid, ask) {
  const b = bid != null ? Number(bid) : null;
  const a = ask != null ? Number(ask) : null;
  if (b != null && a != null) return (b + a) / 2;
  if (b != null) return b;
  if (a != null) return a;
  return null;
}

function _marketPrices(m) {
  if (m.up_mid != null && m.down_mid != null) {
    return { up: Number(m.up_mid).toFixed(2), down: Number(m.down_mid).toFixed(2) };
  }
  const upVal = _bookMid(m.up_bid, m.up_ask);
  const dnVal = _bookMid(m.down_bid, m.down_ask);
  const isDefault = upVal != null && dnVal != null
    && Math.abs(upVal - 0.5) < 0.001 && Math.abs(dnVal - 0.5) < 0.001;
  if (upVal != null && dnVal != null && !isDefault) {
    return { up: upVal.toFixed(2), down: dnVal.toFixed(2) };
  }
  if (!isDefault) {
    if (upVal != null) return { up: upVal.toFixed(2), down: (1 - upVal).toFixed(2) };
    if (dnVal != null) return { up: (1 - dnVal).toFixed(2), down: dnVal.toFixed(2) };
  }
  if (m.up_mid != null) { const v = Number(m.up_mid); return { up: v.toFixed(2), down: (1 - v).toFixed(2) }; }
  if (m.down_mid != null) { const v = Number(m.down_mid); return { up: (1 - v).toFixed(2), down: v.toFixed(2) }; }
  return { up: "--", down: "--" };
}

// ────── Market Grid Renderer (Ladder MM) ──────
function renderMarketGrid(markets, prices) {
  const grid = document.getElementById("market-grid");
  const cardsWrap = document.getElementById("market-grid-cards");
  if (!markets || markets.length === 0) {
    grid.style.display = "none";
    return;
  }
  _lastMarkets = markets;
  grid.style.display = "block";
  cardsWrap.innerHTML = markets.map((m, idx) => {
    const mp = _marketPrices(m);
    const polyPrice = m.current_price != null ? fmtPrice(m.current_price) : "";

    // Ladder-specific fields
    const rungsFilled = (m.rungs_filled != null && m.rungs_total != null)
      ? `${m.rungs_filled}/${m.rungs_total}`
      : (m.rungs_filled != null ? String(m.rungs_filled) : "--");
    const spreadWidth = m.spread_width != null
      ? (Number(m.spread_width) * 100).toFixed(1) + "%"
      : "--";
    const imbalance = m.imbalance != null
      ? (Number(m.imbalance) * 100).toFixed(1) + "%"
      : "--";

    const imbalNum = m.imbalance != null ? Number(m.imbalance) : null;
    const imbalClass = imbalNum == null ? "" : imbalNum < 0.3 ? "imbal-low" : imbalNum < 0.6 ? "imbal-mid" : "imbal-high";

    const pos = m.position;
    const state = pos ? "position" : "scanning";

    let pnlHtml;
    let badgeHtml;
    if (pos) {
      const pnlVal = pos.unrealized_pnl || 0;
      const pnlColor = pnlVal >= 0 ? "var(--accent-green)" : "var(--accent-red)";
      const pnlSign = pnlVal >= 0 ? "+" : "\u2212";
      pnlHtml = `<div class="market-card-pnl">
        <span class="pos-side ${pos.side === "Up" ? "pos-up" : "pos-down"}">${pos.side}</span>
        <span class="pos-size">${pos.size.toFixed(1)} @ ${pos.avg_price.toFixed(2)}</span>
        <span class="pos-pnl" style="color:${pnlColor}">${pnlSign}$${Math.abs(pnlVal).toFixed(2)}</span>
      </div>`;
      badgeHtml = `<span class="market-card-badge active">POSITION</span>`;
    } else {
      pnlHtml = `<div class="market-card-pnl market-card-pnl-empty">
        <span class="pos-pnl">$0.00</span>
      </div>`;
      badgeHtml = `<span class="market-card-badge watching">SCANNING</span>`;
    }

    return `<div class="market-card" data-state="${state}" data-idx="${idx}" style="cursor:pointer">
      <div class="market-card-header">
        <span class="market-card-label">${m.label || m.slug}</span>
        <span class="rungs-badge">${rungsFilled}</span>
      </div>
      <div class="market-card-live">${polyPrice}</div>
      <div class="market-card-book">
        <div class="mc-book-side mc-up">
          <span class="mc-book-label">Up</span>
          <span class="mc-book-price">${mp.up}</span>
        </div>
        <div class="mc-book-side mc-down">
          <span class="mc-book-label">Down</span>
          <span class="mc-book-price">${mp.down}</span>
        </div>
        <div class="mc-spread">
          <span class="mc-book-label">Spread</span>
          <span class="mc-spread-value spread-badge">${spreadWidth}</span>
        </div>
      </div>
      <div class="imbalance-indicator ${imbalClass}">
        <span class="imbal-label">Imbalance</span>
        <span class="imbal-value">${imbalance}</span>
      </div>
      ${pnlHtml}
      <div class="market-card-footer">
        <span class="market-card-target"></span>
        ${badgeHtml}
      </div>
    </div>`;
  }).join("");

  cardsWrap.querySelectorAll(".market-card").forEach(card => {
    card.addEventListener("click", () => {
      const idx = Number(card.dataset.idx);
      if (_lastMarkets[idx]) {
        _openDetailSlug = _lastMarkets[idx].slug;
        showMarketDetail(_lastMarkets[idx]);
      }
    });
  });

  if (_openDetailSlug) {
    const openM = markets.find(m => m.slug === _openDetailSlug);
    if (openM) showMarketDetail(openM);
  }
}

function showMarketDetail(m) {
  const overlay = document.getElementById("market-detail-overlay");
  overlay.style.display = "";
  document.getElementById("md-title").textContent = m.label || m.slug;
  const mdLink = document.getElementById("md-link");
  if (m.slug) {
    mdLink.href = "https://polymarket.com/event/" + encodeURIComponent(m.slug);
    mdLink.style.display = "";
  } else {
    mdLink.style.display = "none";
  }
  document.getElementById("md-question").textContent = m.question || "";
  document.getElementById("md-current-price").textContent = m.current_price != null ? fmtPrice(m.current_price) : "--";

  // Ladder-specific detail fields
  const spreadWidth = m.spread_width != null
    ? (Number(m.spread_width) * 100).toFixed(2) + "%"
    : "--";
  const rungsFilled = (m.rungs_filled != null && m.rungs_total != null)
    ? `${m.rungs_filled}/${m.rungs_total}`
    : (m.rungs_filled != null ? String(m.rungs_filled) : "--");
  const imbalance = m.imbalance != null
    ? (Number(m.imbalance) * 100).toFixed(1) + "%"
    : "--";
  document.getElementById("md-spread-width").textContent = spreadWidth;
  document.getElementById("md-rungs-filled").textContent = rungsFilled;
  document.getElementById("md-imbalance").textContent = imbalance;

  const mdPrices = _marketPrices(m);
  document.getElementById("md-up-ask").textContent = mdPrices.up;
  document.getElementById("md-down-ask").textContent = mdPrices.down;

  const posEl = document.getElementById("md-position");
  if (m.position) {
    const p = m.position;
    const pnlVal = p.unrealized_pnl || 0;
    const pnlColor = pnlVal >= 0 ? "var(--accent-green)" : "var(--accent-red)";
    const pnlSign = pnlVal >= 0 ? "+" : "\u2212";
    posEl.innerHTML = `<div class="md-pos-row">
      <span class="pos-side ${p.side === "Up" ? "pos-up" : "pos-down"}">${p.side}</span>
      <span>${p.size.toFixed(1)} shares @ ${p.avg_price.toFixed(2)}</span>
      <span>Cost: $${p.usdc_cost.toFixed(2)}</span>
      <span style="color:${pnlColor};font-weight:600">PnL: ${pnlSign}$${Math.abs(pnlVal).toFixed(2)}</span>
    </div>`;
    posEl.style.display = "";
  } else {
    posEl.innerHTML = '<span class="trade-empty-row">No position</span>';
    posEl.style.display = "";
  }

  const label = (m.label || "").toLowerCase();
  const tradesEl = document.getElementById("md-trades");
  const marketTrades = _lastActivityFeed.filter(i =>
    (i.kind === "TRADE" || i.kind === "EXIT" || i.kind === "FILL") &&
    i.msg && i.msg.toLowerCase().includes(`[${label}]`)
  );
  if (marketTrades.length === 0) {
    tradesEl.innerHTML = '<div class="trade-empty-row">No trades in this market</div>';
  } else {
    tradesEl.innerHTML = marketTrades.slice(-15).reverse().map(item => {
      const d = new Date((item.ts || 0) * 1000);
      const ts = d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
      const msg = item.msg || "";
      const sideMatch = msg.match(/^(BUY|SELL)/);
      const side = sideMatch ? sideMatch[1] : "?";
      const priceMatch = msg.match(/@ ([\d.]+)/);
      const price = priceMatch ? priceMatch[1] : "";
      const sizeMatch = msg.match(/^(?:BUY|SELL)\s+([\d.]+)/);
      const size = sizeMatch ? Number(sizeMatch[1]).toFixed(1) : "";
      return `<div class="md-trade-row">
        <span>${ts}</span>
        <span class="${side === "BUY" ? "side-buy" : "side-sell"}">${side}</span>
        <span>${size}</span>
        <span>@ ${price}</span>
      </div>`;
    }).join("");
  }
}

document.getElementById("md-close").addEventListener("click", () => {
  _openDetailSlug = null;
  document.getElementById("market-detail-overlay").style.display = "none";
});
document.getElementById("market-detail-overlay").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) {
    _openDetailSlug = null;
    e.currentTarget.style.display = "none";
  }
});

let _prevFeedFingerprint = "";

function _feedFingerprint(feed) {
  if (!feed || feed.length === 0) return "";
  const last = feed[feed.length - 1];
  return `${feed.length}:${last.ts}:${last.kind}`;
}

function renderActivityFeed(feed) {
  const wrap = document.getElementById("activity-feed-list");
  if (!feed || feed.length === 0) {
    if (_prevFeedFingerprint !== "") {
      wrap.innerHTML = '<div class="trade-empty-row">No activity yet</div>';
      _prevFeedFingerprint = "";
    }
    return;
  }

  const fp = _feedFingerprint(feed);
  if (fp === _prevFeedFingerprint) return;
  _prevFeedFingerprint = fp;

  const visible = feed.slice(-30);
  wrap.innerHTML = visible.reverse().map(item => {
    const d = new Date((item.ts || 0) * 1000);
    const ts = d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const kind = item.kind || "INFO";
    return `<div class="activity-item"><span class="activity-time">${ts}</span><span class="activity-kind ${kind}">${kind}</span><span class="activity-msg">${item.msg || ""}</span></div>`;
  }).join("");
}

// ────── Trades Table Renderer ──────
let _prevTradeFingerprint = "";

function renderTradesTable(activityFeed) {
  const thead = document.querySelector("#trades-table thead tr");
  const tbody = document.getElementById("trades-tbody");

  const trades = (activityFeed || []).filter(i =>
    i.kind === "TRADE" || i.kind === "EXIT" || i.kind === "FILL"
  ).reverse();

  if (trades.length === 0) {
    if (_prevTradeFingerprint !== "") {
      tbody.innerHTML = '<tr class="trade-empty-row"><td colspan="6">No trades yet</td></tr>';
      _prevTradeFingerprint = "";
    }
    return;
  }

  const fp = `${trades.length}:${trades[0]?.ts}`;
  if (fp === _prevTradeFingerprint) return;
  _prevTradeFingerprint = fp;

  tbody.innerHTML = trades.slice(0, 30).map((item, idx) => {
    const d = new Date((item.ts || 0) * 1000);
    const ts = d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const msg = item.msg || "";
    const sideMatch = msg.match(/^(BUY|SELL)/);
    const side = sideMatch ? sideMatch[1] : "BUY";
    const sideClass = side === "BUY" ? "side-buy" : "side-sell";
    const bracketMatch = msg.match(/\[([^\]]+)\]/);
    const market = bracketMatch ? bracketMatch[1] : "";
    const priceMatch = msg.match(/@ ([\d.]+)/);
    const price = priceMatch ? priceMatch[1] : "";
    const rungsMatch = msg.match(/rungs?[=:]?\s*([\d]+)/i);
    const rungs = rungsMatch ? rungsMatch[1] : "";
    const sizeMatch = msg.match(/^(?:BUY|SELL)\s+([\d.]+)/);
    const size = sizeMatch ? Number(sizeMatch[1]).toFixed(1) : "";
    const usdMatch = msg.match(/\$([\d.]+)/);
    const usd = usdMatch ? usdMatch[1] : "";
    const rowCls = idx < 3 ? "trade-new" : "";
    return `<tr class="${rowCls}">
      <td>${ts}</td>
      <td><span class="side-badge ${sideClass}">${side}</span></td>
      <td>${market}</td>
      <td>${price}</td>
      <td>${rungs}</td>
      <td>${usd ? "$" + usd : size}</td>
    </tr>`;
  }).join("");
}

// ────── Apply Status (PolyBot payload) ──────
function applyStatus(data) {
  if (data.connected && !agentRunning) {
    onAgentStarted();
  } else if (!data.connected && agentRunning && hadConnected) {
    onAgentStopped();
  }
  updateConnectionStatus(data.connected, data.market_name);
  syncButtons(data.connected);

  // Mode badge
  const badge = document.getElementById("mode-badge");
  badge.textContent = "LADDER MM";
  badge.classList.add("ladder");

  // Activity feed cache
  if (data.activity_feed) _lastActivityFeed = data.activity_feed;

  // Price strips
  renderPriceStrip(data.prices, data.binance_prices);

  // Market grid (ladder info)
  renderMarketGrid(data.active_markets, data.prices);

  // Activity feed
  renderActivityFeed(data.activity_feed);

  // Trades table
  renderTradesTable(data.activity_feed);

  // Hero: PnL
  const r = Number(data.realized_pnl) || 0;
  const u = Number(data.unrealized_pnl) || 0;
  const t = Number(data.total_pnl) || 0;

  const pnlEl = document.getElementById("total-pnl");
  const pnlCard = document.getElementById("hero-pnl");
  pnlEl.textContent = fmtUsd(t);
  pnlEl.style.color = t > 0 ? "var(--accent-green)" : t < 0 ? "var(--accent-red)" : "var(--text-primary)";
  pnlCard.setAttribute("data-glow", t > 0 ? "green" : t < 0 ? "red" : "neutral");

  if (prevTotalPnl !== null && prevTotalPnl !== t) {
    flashEl(pnlEl);
  }
  prevTotalPnl = t;

  if (data.connected) {
    pnlHistory.push(t);
    if (pnlHistory.length > PNL_HISTORY_MAX) pnlHistory.shift();
    renderSparkline(pnlHistory);
  }

  const realEl = document.getElementById("realized-pnl");
  realEl.textContent = fmtUsd(r);
  realEl.style.color = r > 0 ? "var(--accent-green)" : r < 0 ? "var(--accent-red)" : "";

  const unrealEl = document.getElementById("unrealized-pnl");
  unrealEl.textContent = fmtUsd(u);
  unrealEl.style.color = u > 0 ? "var(--accent-green)" : u < 0 ? "var(--accent-red)" : "";

  // Hero: Trades
  const tradeCount = data.trade_count ?? 0;
  document.getElementById("trade-count").textContent = tradeCount;
  document.getElementById("position-count").textContent = data.position_count ?? 0;
  document.getElementById("runtime").textContent = fmtRuntime(data.runtime_sec);

  // Hero: Ladder MM metrics
  document.getElementById("pairs-completed").textContent = data.pairs_completed ?? 0;

  const avgPairCostEl = document.getElementById("avg-pair-cost");
  if (data.avg_pair_cost != null) {
    avgPairCostEl.textContent = "$" + Number(data.avg_pair_cost).toFixed(3);
  } else {
    avgPairCostEl.textContent = "--";
  }

  const imbalanceEl = document.getElementById("imbalance-ratio");
  if (data.imbalance_ratio != null) {
    imbalanceEl.textContent = fmtPct(data.imbalance_ratio);
  } else {
    imbalanceEl.textContent = "--";
  }

  prevTradeCount = tradeCount;
}

// ────── Init ──────
async function init() {
  try {
    const resp = await fetch(`${API_BASE}/api/state`);
    const data = await resp.json();
    if (data.connected) {
      agentRunning = true;
      hadConnected = true;
    }
    applyStatus(data);
    syncButtons(data.connected);
    if (data.connected) {
      connectWebSocket();
      startPolling();
    }
  } catch (e) {
    console.error("Status check failed:", e);
  }
}

init();
