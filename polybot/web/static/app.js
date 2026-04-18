const API_BASE = window.location.origin;
const WS_URL = `ws://${window.location.host}/ws`;
let ws = null;
let wsReconnectTimer = null;
let pollTimer = null;
let agentRunning = false;
let hadConnected = false;
let prevTradeCount = 0;
let prevTotalPnl = null;
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
      pnlHistory.length = 0;
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

// ────── Test Connection ──────
document.getElementById("btn-test-conn").addEventListener("click", async () => {
  const btn = document.getElementById("btn-test-conn");
  const origText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg> Testing...';
  try {
    const resp = await fetch(`${API_BASE}/api/test-connection`, { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
      showToast(`Connection OK — Wallet balance: $${data.balance.toFixed(2)}`, "success");
    } else {
      showToast("Connection failed: " + (data.error || "Unknown error"), "error");
    }
  } catch (e) {
    showToast("Connection test failed: " + e, "error");
  } finally {
    btn.disabled = false;
    btn.innerHTML = origText;
  }
});

// ────── Mode Toggle (Paper/Live) ──────
// NOTE: flipping this control only updates the persisted config (.env).
// The running bot instantiated its clob_client at startup; swapping
// paper<->live mid-run is unsafe (in-flight orders, tracker state, WS
// feeds), so we tell the user a restart is required and let the mode
// badge in the header continue to reflect the actual running bot state.
document.getElementById("mode-switch").addEventListener("change", async (e) => {
  const isPaper = e.target.checked;
  if (!isPaper) {
    // Switching to LIVE — confirm
    const ok = await showModal("Save LIVE mode? This changes the persisted config. You'll be prompted to restart the bot before any real orders are placed.");
    if (!ok) {
      e.target.checked = true; // revert
      return;
    }
  }
  try {
    const resp = await fetch(`${API_BASE}/api/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: isPaper }),
    });
    const data = await resp.json();
    if (data.ok) {
      if (data.restart_required) {
        showToast("Mode saved — restart bot to apply.", "warning");
      } else {
        showToast("Mode saved.", "info");
      }
      // DO NOT update the badge or leave the checkbox optimistically flipped.
      // The next WS tick (applyStatus) will drive the checkbox back to match
      // the actually-running bot's mode, keeping UI and reality aligned.
    } else {
      showToast("Mode switch failed: " + (data.error || "unknown"), "error");
      e.target.checked = !isPaper; // revert
    }
  } catch (err) {
    showToast("Mode switch failed: " + err, "error");
    e.target.checked = !isPaper; // revert
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

function updateConnectionStatus(connected, marketName, cancelOnly) {
  const dot = document.getElementById("live-dot");
  const text = document.getElementById("status-text");
  const trading = connected && !cancelOnly;
  dot.classList.toggle("connected", trading);
  dot.classList.toggle("disconnected", !trading && agentRunning);
  dot.classList.toggle("error", !connected && agentRunning && typeof marketName === "string" && marketName.startsWith("ERROR:"));
  if (trading) {
    text.textContent = "Connected";
  } else if (connected && cancelOnly) {
    text.textContent = "Standby";
  } else if (agentRunning && typeof marketName === "string" && (marketName.startsWith("ERROR:") || marketName.endsWith("..."))) {
    text.textContent = marketName;
  } else {
    text.textContent = "Offline";
  }
}

function syncButtons(connected, cancelOnly) {
  const trading = connected && !cancelOnly;
  document.getElementById("btn-start").disabled = trading;
  document.getElementById("btn-stop").disabled = !trading;
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
  const color = lastVal >= 0 ? "#34d399" : "#f87171";
  const gradColor = lastVal >= 0 ? "rgba(52,211,153,0.25)" : "rgba(248,113,113,0.25)";
  line.setAttribute("stroke", color);
  gradStop.setAttribute("stop-color", gradColor);

  const pathD = `M${points[0]} ` + points.slice(1).map(p => `L${p}`).join(" ") +
    ` L${W},${H} L0,${H} Z`;
  fill.setAttribute("d", pathD);
}

// ────── Price Strip Renderer ──────
function renderPriceStrip(prices) {
  const strip = document.getElementById("price-strip");
  if (!prices || Object.keys(prices).length === 0) {
    strip.style.display = "none";
    return;
  }
  strip.style.display = "grid";
  const syms = ["BTC", "ETH", "SOL", "XRP"];
  const dotColors = { BTC: "var(--orange)", ETH: "var(--blue)", SOL: "var(--purple)", XRP: "var(--cyan)" };
  const items = syms.filter(s => prices[s] != null).map((sym, i) => {
    const p = Number(prices[sym]);
    const borderClass = i > 0 ? ' style="border-left:1px solid rgba(255,255,255,0.06)"' : '';
    return `<div class="price-item"${borderClass}><span class="price-dot" style="background:${dotColors[sym]}"></span><span class="price-name">${sym}</span><span class="price-val">${fmtPrice(p)}</span></div>`;
  }).join("");
  strip.innerHTML = `<span class="price-source">BINANCE SPOT</span>${items}`;
}

// ────── Activity Feed Renderer ──────
function renderActivityFeed(feed) {
  const container = document.getElementById("activity-feed");
  if (!container) return;
  if (!feed || feed.length === 0) {
    container.innerHTML = '<div class="trade-empty-row">No activity yet</div>';
    return;
  }
  const events = feed.filter(e => ["FILL", "LADDER", "PAIR_COMPLETE", "CANCEL", "REBALANCE", "INFO"].includes(e.kind));
  if (events.length === 0) {
    container.innerHTML = '<div class="trade-empty-row">No activity yet</div>';
    return;
  }
  const badgeClasses = {
    FILL: "badge-fill",
    LADDER: "badge-ladder",
    PAIR_COMPLETE: "badge-pair",
    CANCEL: "badge-cancel",
    REBALANCE: "act-rebalance",
    INFO: "act-info"
  };
  container.innerHTML = events.slice(-20).reverse().map(e => {
    const time = new Date(e.ts * 1000).toLocaleTimeString("en-US", {hour12: false, hour: "2-digit", minute: "2-digit"});
    const cls = badgeClasses[e.kind] || "act-info";
    const label = e.kind === "PAIR_COMPLETE" ? "PAIR" : e.kind;
    const msg = (e.msg || "").replace(/[\u2190-\u21FF]/g, ">");
    return `<div class="activity-row"><span class="activity-badge ${cls}">${label}</span><span class="activity-msg">${msg}</span><span class="activity-time">${time}</span></div>`;
  }).join("");
}

// ────── Time formatting ──────
function _fmtTimeLeft(sec) {
  if (sec == null || sec < 0) return "--";
  sec = Math.floor(sec);
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return `${h}h ${rm}m`;
  }
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

function _fmtMidPrice(val) {
  if (val == null) return "--";
  return Number(val).toFixed(2);
}

// ────── Ladder Visualization Builder ──────
function _buildLadderViz(m) {
  const restingUp = m.resting_up || 0;
  const restingDn = m.resting_dn || 0;
  const pos = m.position || {};
  const filledUp = pos.up_qty || 0;
  const filledDn = pos.dn_qty || 0;
  const totalUp = restingUp + (filledUp > 0 ? Math.ceil(filledUp / 3) : 0);
  const totalDn = restingDn + (filledDn > 0 ? Math.ceil(filledDn / 3) : 0);

  if (totalUp === 0 && totalDn === 0) return '';

  const upAvg = pos.up_avg || 0.40;
  const dnAvg = pos.dn_avg || 0.40;
  const maxRungs = Math.max(totalUp, totalDn, 5);
  const numRungs = Math.min(maxRungs, 7);

  function buildSide(side, avg, resting, filledQty, numR) {
    const isUp = side === 'up';
    const barFilled = isUp ? 'up-filled' : 'dn-filled';
    const barResting = isUp ? 'up-resting' : 'dn-resting';
    const tagClass = isUp ? 'filled-up' : 'filled-dn';
    const titleColor = isUp ? 'var(--teal)' : 'var(--red)';

    let html = `<div><div class="ladder-col-title" style="color:${titleColor}">${isUp ? 'UP' : 'DN'} Ladder</div>`;
    // Show filled rungs first, then resting rungs
    const filledCount = filledQty > 0 ? Math.max(1, Math.min(numR, Math.ceil(filledQty / 3))) : 0;
    const restingCount = Math.max(0, numR - filledCount);
    const totalRungs = filledCount + restingCount;
    for (let i = 0; i < totalRungs && i < 7; i++) {
      const price = (avg + (totalRungs - 1 - i) * 0.01).toFixed(2);
      const isFilled = i < filledCount;
      const barWidth = isFilled ? Math.max(30, 100 - i * 10) : Math.max(10, 60 - (i - filledCount) * 6);
      const barCls = isFilled ? barFilled : barResting;
      const tag = isFilled ? `<span class="rung-tag ${tagClass}">F</span>` : '';
      const size = isFilled ? Math.max(1, Math.round(filledQty / filledCount)) : '';
      html += `<div class="ladder-rung"><span class="rung-price">$${price}</span><div class="rung-bar-wrap"><div class="rung-bar ${barCls}" style="width:${barWidth}%"></div></div><span class="rung-size">${size}</span>${tag}</div>`;
    }
    html += '</div>';
    return html;
  }

  let html = '<div class="ladder-viz">';
  html += buildSide('up', upAvg, restingUp, filledUp, Math.min(totalUp || 3, numRungs));
  html += buildSide('dn', dnAvg, restingDn, filledDn, Math.min(totalDn || 3, numRungs));
  html += '</div>';
  return html;
}

// ────── Position Cards Renderer ──────
function renderPositionCards(markets) {
  const container = document.getElementById("position-cards-container");
  if (!container) return;

  if (!markets || markets.length === 0) {
    container.innerHTML = '<div class="pos-empty-state">Discovering markets...</div>';
    return;
  }

  const withPosition = markets.filter(m => m.position && (m.position.up_qty > 0 || m.position.dn_qty > 0));
  const scanning = markets.filter(m => !m.position || (m.position.up_qty === 0 && m.position.dn_qty === 0));

  let html = "";

  for (const m of withPosition) {
    const pos = m.position;
    const timeLeft = _fmtTimeLeft(m.remaining_sec);
    const urgentClass = (m.remaining_sec != null && m.remaining_sec < 60) ? " urgent" : "";

    const upAvg = pos.up_qty > 0 ? (pos.up_avg || 0).toFixed(2) : "--";
    const dnAvg = pos.dn_qty > 0 ? (pos.dn_avg || 0).toFixed(2) : "--";
    const upCost = pos.up_cost != null ? "$" + Number(pos.up_cost).toFixed(2) : "$0.00";
    const dnCost = pos.dn_cost != null ? "$" + Number(pos.dn_cost).toFixed(2) : "$0.00";
    const restingUp = m.resting_up || 0;
    const restingDn = m.resting_dn || 0;

    const profUp = pos.profit_if_up != null ? Number(pos.profit_if_up) : 0;
    const profDn = pos.profit_if_down != null ? Number(pos.profit_if_down) : 0;
    const profUpSign = profUp >= 0 ? "+" : "\u2212";
    const profDnSign = profDn >= 0 ? "+" : "\u2212";
    const profUpClass = profUp >= 0 ? "green" : "red";
    const profDnClass = profDn >= 0 ? "green" : "red";

    const pairCost = pos.pair_cost != null ? "$" + Number(pos.pair_cost).toFixed(3) : "--";
    const imbalance = m.imbalance != null ? (Number(m.imbalance) * 100).toFixed(1) + "%" : "--";
    const budget = m.budget != null ? "$" + Number(m.budget).toFixed(2) : "--";
    const deployed = "$" + (Number(pos.up_cost || 0) + Number(pos.dn_cost || 0)).toFixed(2);

    const polyLink = m.slug ? `https://polymarket.com/event/${encodeURIComponent(m.slug)}` : null;

    // Detect timeframe from label or slug
    const label = m.label || m.slug || "";
    let tfClass = "tf-5m";
    let tfLabel = "5m";
    if (label.includes("15m")) { tfClass = "tf-15m"; tfLabel = "15m"; }
    else if (label.includes("1h")) { tfClass = "tf-1h"; tfLabel = "1h"; }

    // Extract asset from label
    const assetMatch = label.match(/^(BTC|ETH|SOL|XRP)/i);
    const asset = assetMatch ? assetMatch[1].toUpperCase() : label.split(" ")[0];

    // Progress bar
    const tf = m.timeframe || m.remaining_sec || 300;
    const elapsed = m.remaining_sec != null ? Math.max(0, 1 - m.remaining_sec / tf) : 0;
    const elapsedPct = Math.min(100, elapsed * 100);

    // UP Mid / DN Mid / Spread
    const upMid = m.up_mid != null ? Number(m.up_mid).toFixed(2) : "--";
    const dnMid = m.down_mid != null ? Number(m.down_mid).toFixed(2) : "--";
    const spread = (m.up_mid != null && m.down_mid != null)
      ? "$" + Math.abs(1 - Number(m.up_mid) - Number(m.down_mid)).toFixed(2)
      : "--";

    // Target line
    let targetHtml = '';
    if (m.price_to_beat) {
      targetHtml = `<div class="tsd-item"><span class="tsd-label">Target</span><span class="tsd-val mono">${fmtPrice(m.price_to_beat)}</span></div>`;
    }

    html += `<div class="position-card glass">
      <div class="pos-header">
        <div class="pos-header-left">
          <span class="pos-asset">${asset}</span>
          <span class="pos-tf-badge ${tfClass}">${tfLabel}</span>
          ${polyLink ? `<a class="pos-card-link" href="${polyLink}" target="_blank" rel="noopener"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg></a>` : ''}
        </div>
        <span class="pos-timer mono${urgentClass}">${timeLeft}</span>
      </div>
      <div class="progress-track"><div class="progress-fill" style="width:${elapsedPct}%"></div></div>
      <div class="tsd-row">
        ${targetHtml}
        <div class="tsd-item"><span class="tsd-label">UP Mid</span><span class="tsd-val mono" style="color:var(--teal)">$${upMid}</span></div>
        <div class="tsd-item"><span class="tsd-label">DN Mid</span><span class="tsd-val mono" style="color:var(--red)">$${dnMid}</span></div>
        <div class="tsd-item"><span class="tsd-label">Spread</span><span class="tsd-val mono">${spread}</span></div>
      </div>
      ${(() => {
        if (m.fair_value_up == null) return '';
        const pUp = (m.fair_value_up * 100).toFixed(1);
        const pDn = ((1 - m.fair_value_up) * 100).toFixed(1);
        const cert = m.fair_value_certainty ? (m.fair_value_certainty * 100).toFixed(0) : '--';
        const vol = m.fair_value_vol ? (m.fair_value_vol * 100).toFixed(1) : '--';
        const upColor = m.fair_value_up >= 0.5 ? 'var(--teal)' : 'var(--text-dim)';
        const dnColor = m.fair_value_up < 0.5 ? 'var(--red)' : 'var(--text-dim)';
        const phaseColors = { bilateral: '#666', skewed: '#e0a526', directional: '#4ecdc4' };
        const phaseLabel = (m.strategy_phase || 'bilateral').toUpperCase();
        const phaseColor = phaseColors[m.strategy_phase] || '#666';
        return `<div class="tsd-row" style="font-size:0.78rem">
          <div class="tsd-item"><span class="tsd-label">P(UP)</span><span class="tsd-val mono" style="color:${upColor}">${pUp}%</span></div>
          <div class="tsd-item"><span class="tsd-label">P(DN)</span><span class="tsd-val mono" style="color:${dnColor}">${pDn}%</span></div>
          <div class="tsd-item"><span class="tsd-label">Vol</span><span class="tsd-val mono">${vol}%</span></div>
          <div class="tsd-item"><span class="tsd-label">Cert</span><span class="tsd-val mono">${cert}%</span></div>
          <div class="tsd-item"><span class="pos-tf-badge" style="background:${phaseColor};font-size:0.65rem;padding:1px 5px">${phaseLabel}</span></div>
        </div>`;
      })()}
      ${_buildLadderViz(m)}
      <div class="side-summaries">
        <div class="side-box up">
          <div class="side-box-header">
            <span class="side-box-title" style="color:var(--teal)">UP</span>
            <span class="side-box-badge">${restingUp} resting</span>
          </div>
          <div class="side-box-row"><span class="side-box-row-label">Filled</span><span class="side-box-row-val" style="color:var(--teal)">${pos.up_qty.toFixed(1)}</span></div>
          <div class="side-box-row"><span class="side-box-row-label">Avg</span><span class="side-box-row-val">$${upAvg}</span></div>
          <div class="side-box-row"><span class="side-box-row-label">Cost</span><span class="side-box-row-val">${upCost}</span></div>
        </div>
        <div class="side-box dn">
          <div class="side-box-header">
            <span class="side-box-title" style="color:var(--red)">DOWN</span>
            <span class="side-box-badge">${restingDn} resting</span>
          </div>
          <div class="side-box-row"><span class="side-box-row-label">Filled</span><span class="side-box-row-val" style="color:var(--red)">${pos.dn_qty.toFixed(1)}</span></div>
          <div class="side-box-row"><span class="side-box-row-label">Avg</span><span class="side-box-row-val">$${dnAvg}</span></div>
          <div class="side-box-row"><span class="side-box-row-label">Cost</span><span class="side-box-row-val">${dnCost}</span></div>
        </div>
      </div>
      <div class="projections">
        <div class="proj-box win">
          <div class="proj-label">If UP wins</div>
          <div class="proj-value ${profUpClass}">${profUpSign}$${Math.abs(profUp).toFixed(2)}</div>
        </div>
        <div class="proj-box lose">
          <div class="proj-label">If DN wins</div>
          <div class="proj-value ${profDnClass}">${profDnSign}$${Math.abs(profDn).toFixed(2)}</div>
        </div>
      </div>
      <div class="pos-bottom">
        <div class="pos-bottom-item"><span class="pos-bottom-label">Pair Cost</span><span class="pos-bottom-val">${pairCost}</span></div>
        <div class="pos-bottom-item"><span class="pos-bottom-label">Imbalance</span><span class="pos-bottom-val">${imbalance}</span></div>
        <div class="pos-bottom-item"><span class="pos-bottom-label">Deployed</span><span class="pos-bottom-val">${deployed}</span></div>
        <div class="pos-bottom-item"><span class="pos-bottom-label">Budget</span><span class="pos-bottom-val">${budget}</span></div>
      </div>
    </div>`;
  }

  // Scanning rows
  if (scanning.length > 0) {
    for (const m of scanning) {
      const upMid = _fmtMidPrice(m.up_mid);
      const dnMid = _fmtMidPrice(m.down_mid);
      const totalResting = (m.resting_up || 0) + (m.resting_dn || 0);
      const timeLeft = _fmtTimeLeft(m.remaining_sec);

      const isUpcoming = m.window_status === "upcoming" || m.window_status === "pre_open" || (m.opens_in_sec != null && m.opens_in_sec > 0);
      let badgeHtml;
      if (isUpcoming) {
        badgeHtml = '<span class="scan-badge next">NEXT</span>';
      } else if (totalResting > 0) {
        badgeHtml = '<span class="scan-badge active">ACTIVE</span>';
      } else {
        badgeHtml = '<span class="scan-badge scan-scanning">SCANNING</span>';
      }

      const scanLabel = m.label || m.slug || "";
      const assetMatch = scanLabel.match(/^(BTC|ETH|SOL|XRP)/i);
      const scanAsset = assetMatch ? assetMatch[1].toUpperCase() : scanLabel.split(" ")[0];
      let tfPart = "5m";
      if (scanLabel.includes("15m")) tfPart = "15m";
      else if (scanLabel.includes("1h")) tfPart = "1h";

      html += `<div class="scan-row glass">
        <div class="scan-left">
          ${badgeHtml}
          <span class="scan-info"><strong>${scanAsset}</strong> ${tfPart} &mdash; ${upMid}/${dnMid}</span>
        </div>
        <span class="scan-timer">${timeLeft}</span>
      </div>`;
    }
  }

  if (withPosition.length === 0 && scanning.length === 0) {
    html = '<div class="pos-empty-state">Discovering markets...</div>';
  }

  container.innerHTML = html;
}

// ────── Settlement History Renderer ──────
let _prevSettlementFingerprint = "";

function renderSettlementHistory(data) {
  const listEl = document.getElementById("settlement-list");
  const totalEl = document.getElementById("settlement-total-pnl");
  if (!listEl || !totalEl) return;

  const settlements = data.settlement_history || [];

  if (settlements.length === 0) {
    if (_prevSettlementFingerprint !== "") {
      listEl.innerHTML = '<div class="trade-empty-row">No settlements yet</div>';
      totalEl.textContent = "$0.00";
      totalEl.className = "settle-total";
      _prevSettlementFingerprint = "";
    }
    return;
  }

  // Fingerprint check
  const fp = `${settlements.length}:${settlements[settlements.length - 1].ts}`;
  if (fp === _prevSettlementFingerprint) return;
  _prevSettlementFingerprint = fp;

  // Compute running total
  let totalPnl = 0;
  for (const s of settlements) {
    totalPnl += Number(s.pnl) || 0;
  }

  // Format total
  const totalSign = totalPnl >= 0 ? "+" : "\u2212";
  totalEl.textContent = totalSign + "$" + Math.abs(totalPnl).toFixed(2);
  totalEl.className = "settle-total " + (totalPnl >= 0 ? "" : "red");

  // Render rows (newest first)
  const rows = settlements.slice().reverse().map(item => {
    const d = new Date((item.ts || 0) * 1000);
    const ts = d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit" });
    const asset = item.asset || "";
    const tf = item.timeframe || "";
    const outcome = item.outcome || "";
    const outcomeText = outcome ? `${outcome} won` : "";
    const outcomeClass = outcome === "UP" ? "up-won" : outcome === "DOWN" ? "dn-won" : "";
    const upQty = item.up_qty != null ? item.up_qty : 0;
    const dnQty = item.dn_qty != null ? item.dn_qty : 0;
    const totalCost = item.total_cost != null ? item.total_cost : 0;
    const pairCostVal = item.pair_cost != null ? item.pair_cost.toFixed(3) : "--";
    const pnlVal = Number(item.pnl) || 0;
    const pnlSign = pnlVal >= 0 ? "+" : "\u2212";
    const pnlClass = pnlVal >= 0 ? "green" : "red";
    const accentClass = pnlVal >= 0 ? "green" : "red";

    const oneSided = (upQty === 0 || dnQty === 0) && (upQty > 0 || dnQty > 0);
    const detail = oneSided
      ? `${upQty}UP ${dnQty}DN \u00b7 one-sided`
      : `${upQty}UP ${dnQty}DN \u00b7 cost $${totalCost.toFixed(2)} \u00b7 pair ${pairCostVal}`;

    return `<div class="settle-row">
      <div class="settle-accent ${accentClass}"></div>
      <span class="settle-time">${ts}</span>
      <div class="settle-body">
        <div class="settle-body-top">
          <span class="settle-asset">${asset}</span>
          <span class="settle-tf">${tf}</span>
          ${outcomeText ? `<span class="settle-outcome ${outcomeClass}">${outcomeText}</span>` : ''}
        </div>
        <div class="settle-detail">${detail}</div>
      </div>
      <span class="settle-pnl ${pnlClass}">${pnlSign}$${Math.abs(pnlVal).toFixed(2)}</span>
    </div>`;
  });

  listEl.innerHTML = rows.join("");
}

// ────── Risk Bar Renderer ──────
function renderRiskBar(data) {
  const exposure = data.exposure_factor ?? 1.0;
  const dailyPnl = data.daily_pnl ?? 0;
  const capitalPct = data.capital_at_risk_pct ?? 0;
  const halted = data.is_halted ?? false;

  function setRiskPill(id, text, level) {
    const el = document.getElementById(id);
    if (!el) return;
    const dotClass = level === "danger" ? "red" : level === "warn" ? "amber" : "green";
    el.className = "risk-pill " + (level === "danger" ? "danger" : level === "warn" ? "warn" : "green");
    el.innerHTML = `<span class="risk-dot ${dotClass}"></span><span>${text}</span>`;
  }

  setRiskPill("risk-exposure", `Exposure: ${Math.round(exposure * 100)}%`, exposure < 1 ? "warn" : "ok");
  setRiskPill("risk-daily-pnl", `Daily: ${fmtUsd(dailyPnl)}`, dailyPnl < -20 ? "warn" : "ok");
  setRiskPill("risk-capital", `Capital: ${capitalPct.toFixed(0)}%`, capitalPct > 35 ? "warn" : "ok");
  setRiskPill("risk-status", halted ? "HALTED" : "NORMAL", halted ? "danger" : "ok");
}

// ────── Per-Asset PnL ──────
function renderAssetPnl(data) {
  const pnlMap = data.per_asset_pnl || {};
  const pairsMap = data.per_asset_pairs || {};
  for (const sym of ["BTC", "ETH", "SOL", "XRP"]) {
    const pnl = Number(pnlMap[sym]) || 0;
    const pairs = Number(pairsMap[sym]) || 0;
    const pnlEl = document.getElementById("asset-pnl-" + sym.toLowerCase());
    const pairsEl = document.getElementById("asset-pairs-" + sym.toLowerCase());
    if (pnlEl) {
      pnlEl.textContent = fmtUsd(pnl);
      pnlEl.style.color = pnl > 0 ? "var(--green)" : pnl < 0 ? "var(--red)" : "var(--text-muted)";
    }
    if (pairsEl) {
      pairsEl.textContent = pairs + (pairs === 1 ? " pair" : " pairs");
    }
  }
}

// ────── Equity Curve ──────
const settlementPoints = [];
function renderEquityCurve(data) {
  const settlements = data.settlement_history || [];
  renderPnlChart(settlements);
}

function renderPnlChart(settlements) {
  const svg = document.getElementById("equity-curve");
  const wrap = document.getElementById("equity-curve-wrap");
  if (!svg || !wrap) return;
  if (settlements.length === 0) return;

  // Build cumulative PnL points from full settlement history
  settlementPoints.length = 0;
  let cum = 0;
  for (const s of settlements) {
    const pnl = Number(s.pnl) || 0;
    cum += pnl;
    const tf = s.timeframe || s.meta?.timeframe || "";
    const asset = s.asset || s.meta?.asset || "";
    const outcome = s.outcome || s.meta?.outcome || "";
    settlementPoints.push({ ts: s.ts, pnl: cum, raw: pnl, timeframe: tf, asset: asset, outcome: outcome });
  }

  const W = 900, H = 200, padL = 60, padR = 20, padT = 20, padB = 30;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const vals = settlementPoints.map(p => p.pnl);
  const minV = Math.min(...vals, 0), maxV = Math.max(...vals, 1);
  const range = maxV - minV || 1;
  const zeroY = padT + plotH * (1 - (0 - minV) / range);

  function toXY(i) {
    const x = padL + (i / Math.max(settlementPoints.length - 1, 1)) * plotW;
    const y = padT + plotH * (1 - (settlementPoints[i].pnl - minV) / range);
    return [x, y];
  }

  let linePoints = settlementPoints.map((_, i) => toXY(i).join(",")).join(" ");
  const first = toXY(0), last = toXY(settlementPoints.length - 1);

  // Green fill above zero
  let greenPath = `M${first[0]},${Math.min(first[1], zeroY)}`;
  for (let i = 0; i < settlementPoints.length; i++) {
    const [x, y] = toXY(i);
    greenPath += ` L${x},${Math.min(y, zeroY)}`;
  }
  greenPath += ` L${last[0]},${zeroY} L${first[0]},${zeroY} Z`;

  // Red fill below zero
  let redPath = `M${first[0]},${Math.max(first[1], zeroY)}`;
  for (let i = 0; i < settlementPoints.length; i++) {
    const [x, y] = toXY(i);
    redPath += ` L${x},${Math.max(y, zeroY)}`;
  }
  redPath += ` L${last[0]},${zeroY} L${first[0]},${zeroY} Z`;

  // Markers (small dots on each settlement)
  let markers = settlementPoints.map((p, i) => {
    const [x, y] = toXY(i);
    const col = p.raw >= 0 ? "#34d399" : "#f87171";
    return `<circle cx="${x}" cy="${y}" r="3" fill="var(--bg,#080c18)" stroke="${col}" stroke-width="1.5" class="eq-dot" data-idx="${i}"/>`;
  }).join("");

  // Current value endpoint
  const [cx, cy] = toXY(settlementPoints.length - 1);
  const curVal = settlementPoints[settlementPoints.length - 1].pnl;

  // Invisible hover zones (wider hit areas for each point)
  let hoverZones = settlementPoints.map((p, i) => {
    const [x, y] = toXY(i);
    return `<circle cx="${x}" cy="${y}" r="12" fill="transparent" class="eq-hover-zone" data-idx="${i}"/>`;
  }).join("");

  svg.innerHTML = `
    <defs>
      <linearGradient id="eq-green" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="rgba(52,211,153,0.3)"/><stop offset="100%" stop-color="rgba(52,211,153,0.02)"/></linearGradient>
      <linearGradient id="eq-red" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="rgba(248,113,113,0.02)"/><stop offset="100%" stop-color="rgba(248,113,113,0.25)"/></linearGradient>
    </defs>
    <line x1="${padL}" y1="${zeroY}" x2="${W-padR}" y2="${zeroY}" stroke="rgba(255,255,255,0.15)" stroke-width="1" stroke-dasharray="6,4"/>
    <text x="${padL-8}" y="${zeroY+4}" text-anchor="end" fill="rgba(255,255,255,0.3)" font-family="JetBrains Mono,monospace" font-size="10">$0</text>
    <text x="${padL-8}" y="${padT+10}" text-anchor="end" fill="rgba(255,255,255,0.25)" font-family="JetBrains Mono,monospace" font-size="10">${fmtUsd(maxV)}</text>
    ${minV < 0 ? `<text x="${padL-8}" y="${H-padB}" text-anchor="end" fill="rgba(255,255,255,0.25)" font-family="JetBrains Mono,monospace" font-size="10">${fmtUsd(minV)}</text>` : ''}
    <path d="${greenPath}" fill="url(#eq-green)"/>
    <path d="${redPath}" fill="url(#eq-red)"/>
    <polyline points="${linePoints}" fill="none" stroke="#34d399" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    ${markers}
    <circle cx="${cx}" cy="${cy}" r="6" fill="#34d399" stroke="var(--bg,#080c18)" stroke-width="2"/>
    <text x="${cx-8}" y="${cy-12}" text-anchor="end" fill="#34d399" font-family="JetBrains Mono,monospace" font-size="11" font-weight="600">${fmtUsd(curVal)}</text>
    ${hoverZones}
  `;

  // Tooltip on hover
  _setupChartTooltip(wrap, svg);
}

function _setupChartTooltip(wrap, svg) {
  // Create or reuse tooltip element
  let tip = wrap.querySelector(".eq-tooltip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "eq-tooltip";
    wrap.style.position = "relative";
    wrap.appendChild(tip);
  }

  svg.addEventListener("mousemove", (e) => {
    const zone = e.target.closest(".eq-hover-zone");
    if (!zone) { tip.style.display = "none"; return; }
    const idx = parseInt(zone.dataset.idx);
    const p = settlementPoints[idx];
    if (!p) return;

    const d = new Date(p.ts * 1000);
    const time = d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit" });
    const date = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    const sign = p.raw >= 0 ? "+" : "";

    tip.innerHTML = `
      <div style="font-size:10px;opacity:0.6">${date} ${time}</div>
      <div style="font-size:13px;font-weight:600;color:${p.pnl >= 0 ? '#34d399' : '#f87171'}">${fmtUsd(p.pnl)} total</div>
      <div style="font-size:11px;color:${p.raw >= 0 ? '#34d399' : '#f87171'}">${sign}${fmtUsd(p.raw)} ${p.asset} ${p.timeframe} ${p.outcome}</div>
    `;
    tip.style.display = "block";

    // Position tooltip near the point
    const rect = svg.getBoundingClientRect();
    const cx = parseFloat(zone.getAttribute("cx"));
    const cy = parseFloat(zone.getAttribute("cy"));
    const scaleX = rect.width / 900;
    const scaleY = rect.height / 200;
    let left = cx * scaleX - 60;
    if (left < 0) left = 0;
    if (left > rect.width - 140) left = rect.width - 140;
    tip.style.left = left + "px";
    tip.style.top = (cy * scaleY - 60) + "px";
  });

  svg.addEventListener("mouseleave", () => { tip.style.display = "none"; });
}

// ────── Risk Analytics ──────
function renderRiskAnalytics(feed) {
  const el = document.getElementById("risk-analytics");
  if (!el) return;
  const settles = (feed || []).filter(e => e.kind === "SETTLE" && e.meta);
  if (settles.length === 0) return;

  // Pair cost buckets
  const buckets = [0,0,0,0,0]; // <0.80, 0.80-0.85, 0.85-0.90, 0.90-0.95, >0.95
  const byTf = {};
  let maxDd = 0, bestSettle = 0, cum = 0, peak = 0;
  const startTs = settles.length ? settles[0].ts : Date.now()/1000;
  const endTs = settles.length ? settles[settles.length-1].ts : startTs;

  for (const s of settles) {
    const pc = s.meta.pair_cost;
    const pnl = Number(s.pnl) || 0;
    const tf = s.meta.timeframe || "?";
    cum += pnl;
    peak = Math.max(peak, cum);
    maxDd = Math.min(maxDd, cum - peak);
    bestSettle = Math.max(bestSettle, pnl);

    if (pc && pc > 0) {
      if (pc < 0.80) buckets[0]++;
      else if (pc < 0.85) buckets[1]++;
      else if (pc < 0.90) buckets[2]++;
      else if (pc < 0.95) buckets[3]++;
      else buckets[4]++;
    }
    if (!byTf[tf]) byTf[tf] = {pairs:0, pnl:0, pcs:[]};
    byTf[tf].pairs++;
    byTf[tf].pnl += pnl;
    if (pc > 0) byTf[tf].pcs.push(pc);
  }

  const maxBucket = Math.max(...buckets, 1);
  const bucketLabels = ["<$0.80","$0.80-85","$0.85-90","$0.90-95",">$0.95"];
  const hours = Math.max((endTs - startTs) / 3600, 0.1);
  const pairsHr = (settles.length / hours).toFixed(1);

  let html = `<div class="risk-analytics-title">Risk Analytics</div>`;
  html += `<div class="ra-section-label">Pair Cost Distribution</div><div class="ra-buckets">`;
  for (let i = 0; i < 5; i++) {
    const pct = (buckets[i] / maxBucket * 100).toFixed(0);
    const col = i < 4 ? "var(--green)" : "var(--red)";
    html += `<div class="ra-bucket"><span class="ra-bucket-label">${bucketLabels[i]}</span><div class="ra-bucket-bar-bg"><div class="ra-bucket-bar" style="width:${pct}%;background:${col}"></div></div><span class="ra-bucket-count">${buckets[i]}</span></div>`;
  }
  html += `</div>`;

  html += `<div class="ra-section-label" style="margin-top:12px">Per Timeframe</div><table class="ra-table"><tr><th>TF</th><th>Pairs</th><th>Avg PC</th><th>PnL</th></tr>`;
  for (const tf of ["15m","1h","5m"]) {
    if (!byTf[tf]) continue;
    const d = byTf[tf];
    const avgPc = d.pcs.length ? (d.pcs.reduce((a,b)=>a+b,0)/d.pcs.length).toFixed(3) : "--";
    html += `<tr><td>${tf}</td><td>${d.pairs}</td><td>$${avgPc}</td><td style="color:${d.pnl>=0?'var(--green)':'var(--red)'}">${fmtUsd(d.pnl)}</td></tr>`;
  }
  html += `</table>`;

  html += `<div class="ra-stats"><span>Max DD: <b style="color:var(--red)">${fmtUsd(maxDd)}</b></span><span>Best: <b style="color:var(--green)">${fmtUsd(bestSettle)}</b></span><span>Pairs/hr: <b>${pairsHr}</b></span></div>`;
  el.innerHTML = html;
}

// ────── Sound Alerts ──────
let soundEnabled = localStorage.getItem("polybot_sound") !== "false";
let prevFeedLen = 0;
const audioCtx = typeof AudioContext !== "undefined" ? new AudioContext() : null;

function playTone(freq, dur, type) {
  if (!audioCtx || !soundEnabled) return;
  if (audioCtx.state === "suspended") audioCtx.resume();
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = type || "sine";
  osc.frequency.value = freq;
  gain.gain.value = 0.1;
  gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + dur);
  osc.connect(gain).connect(audioCtx.destination);
  osc.start();
  osc.stop(audioCtx.currentTime + dur);
}

function checkSoundAlerts(feed) {
  if (!feed || feed.length <= prevFeedLen) { prevFeedLen = (feed||[]).length; return; }
  const newEvents = feed.slice(prevFeedLen);
  prevFeedLen = feed.length;
  for (const e of newEvents) {
    if (e.kind === "FILL") playTone(800, 0.1, "sine");
    else if (e.kind === "SETTLE") {
      const pnl = Number(e.pnl) || 0;
      playTone(pnl >= 0 ? 1200 : 400, 0.3, "triangle");
    }
  }
}

(function initSoundToggle() {
  const btn = document.getElementById("sound-toggle");
  if (!btn) return;
  btn.classList.toggle("muted", !soundEnabled);
  btn.addEventListener("click", () => {
    soundEnabled = !soundEnabled;
    localStorage.setItem("polybot_sound", soundEnabled);
    btn.classList.toggle("muted", !soundEnabled);
    showToast(soundEnabled ? "Sound on" : "Sound off", "info");
  });
})();

// ────── Apply Status (PolyBot payload) ──────
function applyStatus(data) {
  const cancelOnly = data.cancel_only_mode;
  const trading = data.connected && !cancelOnly;
  if (data.connected && !agentRunning) {
    agentRunning = true;
    hadConnected = true;
    connectWebSocket();
    startPolling();
  } else if (!data.connected && agentRunning && hadConnected) {
    onAgentStopped();
  }
  updateConnectionStatus(data.connected, data.market_name, cancelOnly);
  syncButtons(data.connected, cancelOnly);

  // Price staleness banner
  const staleBanner = document.getElementById("price-stale-banner");
  if (staleBanner) {
    staleBanner.style.display = data.price_feed_stale ? "block" : "none";
  }

  // Stale order alert banner
  const staleOrderBanner = document.getElementById("stale-order-banner");
  if (staleOrderBanner) {
    const alertMsg = data.stale_order_alert || "";
    if (alertMsg) {
      staleOrderBanner.textContent = alertMsg;
      staleOrderBanner.style.display = "block";
    } else {
      staleOrderBanner.style.display = "none";
    }
  }

  // USDC balance (header + hero)
  const balEl = document.getElementById("usdc-balance");
  const heroUsdcEl = document.getElementById("hero-usdc");
  const usdcVal = Number(data.usdc_balance) || 0;
  if (balEl && data.usdc_balance != null) {
    balEl.textContent = "USDC: $" + usdcVal.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  if (heroUsdcEl) {
    heroUsdcEl.textContent = "$" + usdcVal.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    heroUsdcEl.style.color = "var(--text)";
  }

  // Mode badge in header
  const badge = document.getElementById("mode-badge");
  const modeSwitch = document.getElementById("mode-switch");
  const testConnBtn = document.getElementById("btn-test-conn");
  const cfgDryRun = document.getElementById("s_dry_run");
  if (data.mode === "dry_run") {
    badge.textContent = "PAPER";
    badge.className = "mode-pill";
    if (modeSwitch) modeSwitch.checked = true;
    if (cfgDryRun) cfgDryRun.checked = true;
    if (testConnBtn) testConnBtn.style.display = "none";
  } else {
    badge.textContent = "LIVE";
    badge.className = "mode-pill badge-live";
    if (cfgDryRun) cfgDryRun.checked = false;
    if (modeSwitch) modeSwitch.checked = false;
    if (testConnBtn) testConnBtn.style.display = "";
  }

  // Reset-paper button visibility — hooked into every state update so it
  // toggles as soon as the user flips mode in settings.
  try { updateRestartResetVisibility(data); } catch (e) { /* no-op */ }

  // Live bankroll UX: disable + live-populate from on-chain balance.
  try { updateBankrollFieldForMode(data); } catch (e) { /* no-op */ }

  // Config status card (mode / balance / wallet / creds).
  try { updateConfigStatusCard(data); } catch (e) { /* no-op */ }

  // Mode-mismatch banner: .env says one thing, bot is running another.
  // Surfaces to the user that a restart is needed to apply saved config.
  const mismatchBanner = document.getElementById("mode-mismatch-banner");
  if (mismatchBanner) {
    const configured = data.configured_mode;
    const running = data.mode;
    if (configured && running && configured !== running) {
      const want = configured === "live" ? "LIVE" : "PAPER";
      const have = running === "live" ? "LIVE" : "PAPER";
      const txt = document.getElementById("mode-mismatch-text");
      if (txt) txt.textContent = `Restart required — saved config is ${want}, bot is running as ${have}.`;
      mismatchBanner.style.display = "block";
    } else {
      mismatchBanner.style.display = "none";
    }
  }

  // Price strip — read precomputed binance_spot_values (see _ui_binance_spot_values
  // in bot.py and feedback_ui_binance_spot_recurring_bug.md).
  // Must be direct Binance WS, NOT the RTDS/Chainlink-blended spots dict — that
  // defeats the whole arb edge.
  renderPriceStrip(data.binance_spot_values || data.binance_prices || data.spots, null);

  // Position cards
  renderPositionCards(data.active_markets);

  // Settlement history
  renderSettlementHistory(data);

  // Per-asset PnL
  renderAssetPnl(data);

  // Hero: PnL
  const r = Number(data.realized_pnl) || 0;
  const u = Number(data.unrealized_pnl) || 0;
  const t = Number(data.total_pnl) || 0;

  const pnlEl = document.getElementById("total-pnl");
  pnlEl.textContent = fmtUsd(t);
  pnlEl.style.color = t > 0 ? "var(--green)" : t < 0 ? "var(--red)" : "";

  if (prevTotalPnl !== null && prevTotalPnl !== t) {
    flashEl(pnlEl);
  }
  prevTotalPnl = t;

  // Use backend pnl_series for the graph (complete history, not just recent)
  const series = data.pnl_series || [];
  if (series.length > 0) {
    renderPnlChart(series);
  } else if (data.connected) {
    // Fallback to in-memory history
    pnlHistory.push(t);
    if (pnlHistory.length > PNL_HISTORY_MAX) pnlHistory.shift();
    renderSparkline(pnlHistory);
  }

  const realEl = document.getElementById("realized-pnl");
  realEl.textContent = fmtUsd(r);
  realEl.style.color = r > 0 ? "var(--green)" : r < 0 ? "var(--red)" : "";

  const unrealEl = document.getElementById("unrealized-pnl");
  unrealEl.textContent = fmtUsd(u);
  unrealEl.style.color = u > 0 ? "var(--green)" : u < 0 ? "var(--red)" : "";

  // Trade count (hidden)
  const tradeCount = data.trade_count ?? 0;
  const tradeCountEl = document.getElementById("trade-count");
  if (tradeCountEl) tradeCountEl.textContent = tradeCount;

  // Runtime
  const runtimeSec = data.runtime_sec || 0;
  const runtimeEl = document.getElementById("runtime");
  if (runtimeEl) {
    runtimeEl.textContent = fmtRuntime(runtimeSec);
    runtimeEl.style.color = "var(--text-muted)";
  }

  // Pairs settled
  const wins = data.settled_wins ?? 0;
  const losses = data.settled_losses ?? 0;
  const totalPairs = wins + losses;
  document.getElementById("pairs-settled").textContent = totalPairs > 0 ? `${wins}W / ${losses}L` : "0";

  // Pairs per hour
  const pairsPerHourEl = document.getElementById("pairs-per-hour");
  if (pairsPerHourEl) {
    if (runtimeSec > 0 && totalPairs > 0) {
      const pph = totalPairs / runtimeSec * 3600;
      pairsPerHourEl.textContent = pph.toFixed(1);
    } else {
      pairsPerHourEl.textContent = "0.0";
    }
    pairsPerHourEl.style.color = "var(--text-muted)";
  }

  // Active positions count
  const posCountEl = document.getElementById("position-count");
  if (posCountEl) {
    const posCount = data.position_count ?? 0;
    posCountEl.textContent = posCount;
    posCountEl.style.color = "var(--blue)";
  }

  // Avg pair cost
  const avgPC = data.avg_pair_cost ?? 0;
  const avgPairCostEl = document.getElementById("avg-pair-cost");
  if (avgPC > 0) {
    avgPairCostEl.textContent = "$" + Number(avgPC).toFixed(3);
    avgPairCostEl.style.color = avgPC < 1.0 ? "var(--green)" : "var(--red)";
  } else {
    avgPairCostEl.textContent = "--";
    avgPairCostEl.style.color = "";
  }

  // Profit per pair
  const profitPerPairEl = document.getElementById("profit-per-pair");
  if (totalPairs > 0 && r !== 0) {
    const perPair = r / totalPairs;
    profitPerPairEl.textContent = fmtUsd(perPair);
    profitPerPairEl.style.color = perPair >= 0 ? "var(--green)" : "var(--red)";
  } else {
    profitPerPairEl.textContent = "--";
    profitPerPairEl.style.color = "";
  }

  // Best pair cost (hidden)
  const bestPCEl = document.getElementById("best-pair-cost");
  if (bestPCEl) {
    if (data.best_pair_cost > 0) {
      bestPCEl.textContent = "$" + Number(data.best_pair_cost).toFixed(3);
    } else {
      bestPCEl.textContent = "--";
    }
  }

  // Capital deployed bar
  const capitalPct = data.capital_at_risk_pct ?? 0;
  const capitalBarText = document.getElementById("capital-bar-text");
  const capitalBarFill = document.getElementById("capital-bar-fill");
  if (capitalBarText && capitalBarFill) {
    const bankroll = usdcVal > 0 ? usdcVal : (Number(data.bankroll) || 0);
    const deployed = bankroll * capitalPct / 100;
    capitalBarText.textContent = `$${Math.round(deployed)} / $${Math.round(bankroll)} (${capitalPct.toFixed(0)}%)`;
    capitalBarFill.style.width = Math.min(100, capitalPct) + "%";
  }

  // Activity feed
  renderActivityFeed(data.activity_feed);

  // Equity curve
  renderEquityCurve(data);

  // Risk analytics
  renderRiskAnalytics(data.activity_feed);

  // Sound alerts
  checkSoundAlerts(data.activity_feed);

  // Risk bar
  renderRiskBar(data);

  prevTradeCount = tradeCount;
}

// ────── Tab Switching ──────
document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    const panelId = "panel-" + tab.id.replace("tab-", "");
    const panel = document.getElementById(panelId);
    if (panel) panel.classList.add("active");
  });
});

// ────── Settings Load/Save ──────
async function loadSettings() {
  try {
    const resp = await fetch(`${API_BASE}/api/settings`);
    const s = await resp.json();
    for (const [key, val] of Object.entries(s)) {
      const el = document.getElementById("s_" + key);
      if (!el) continue;
      if (el.type === "checkbox") el.checked = !!val;
      else el.value = val;
    }
    syncConfigUI(s);
  } catch (e) {
    console.error("Load settings failed:", e);
  }
}

// ────── Config Panel Wiring ──────
const TIER_DEFS = [
  { name: "Micro",    min: 0,    max: 200,  cls: "micro",    assets: 1, tfs: ["15m"],          concurrent: 2, fraction: "15%" },
  { name: "Small",    min: 200,  max: 400,  cls: "small",    assets: 1, tfs: ["15m"],          concurrent: 3, fraction: "10%" },
  { name: "Medium",   min: 400,  max: 2000, cls: "medium",   assets: 2, tfs: ["15m","1h"],     concurrent: 4, fraction: "10%" },
  { name: "Standard", min: 2000, max: 1e9,  cls: "standard", assets: 4, tfs: ["5m","15m","1h"], concurrent: 8, fraction: "2-5%" },
];

function getTier(bankroll) {
  return TIER_DEFS.find(t => bankroll >= t.min && bankroll < t.max) || TIER_DEFS[3];
}

function syncConfigUI(s) {
  const bankroll = s.bankroll || 500;
  const tier = getTier(bankroll);

  // Tier badge
  const panel = document.getElementById("tier-panel");
  TIER_DEFS.forEach(t => panel.classList.remove("tier-" + t.cls));
  panel.classList.add("tier-" + tier.cls);

  const badge = document.getElementById("tier-badge");
  badge.textContent = tier.name.toUpperCase();
  badge.className = "tier-badge " + tier.cls;

  // Bankroll display
  document.getElementById("tier-bankroll-display").textContent = "$" + bankroll.toLocaleString();

  // Profile grid
  const grid = document.getElementById("tier-profile-grid");
  const assets = ["BTC","ETH","SOL","XRP"];
  const enabledAssets = assets.filter(a => s["trade_" + a.toLowerCase()]);
  const tfs = [
    { key: "5m", label: "5m", enabled: s.trade_5m },
    { key: "15m", label: "15m", enabled: s.trade_15m },
    { key: "1h", label: "1h", enabled: s.trade_1h },
  ];
  const bestTf = "1h";
  grid.innerHTML = `
    <div class="profile-item">
      <div class="profile-label">Markets</div>
      <div class="profile-value">${assets.map(a =>
        `<span class="asset-pill${enabledAssets.includes(a) ? "" : " disabled"}">${a}</span>`
      ).join("")}</div>
    </div>
    <div class="profile-item">
      <div class="profile-label">Timeframes</div>
      <div class="profile-value">${tfs.map(t =>
        `<span class="tf-pill${t.enabled ? "" : " disabled"}${t.key === bestTf ? " recommended" : ""}">${t.label}</span>`
      ).join("")}</div>
    </div>
    <div class="profile-item">
      <div class="profile-label">Concurrent</div>
      <div class="profile-value"><span class="mono">${tier.concurrent}</span> markets</div>
    </div>
    <div class="profile-item">
      <div class="profile-label">Position Size</div>
      <div class="profile-value"><span class="mono">${tier.fraction}</span> of bankroll</div>
    </div>
  `;

  // Recommendation
  const rec = document.getElementById("tier-recommendation");
  if (bankroll < 400) {
    rec.innerHTML = `<span class="rec-icon">*</span> <strong>At $${bankroll}:</strong> Focus on <strong>1h markets</strong> for best pair costs. Add 15m for more volume once above $400.`;
  } else if (bankroll < 2000) {
    rec.innerHTML = `<span class="rec-icon">*</span> <strong>At $${bankroll}:</strong> Run <strong>15m + 1h</strong> on BTC + ETH. 1h has best fills. Add 5m and more assets above $2,000.`;
  } else {
    rec.innerHTML = `<span class="rec-icon">*</span> <strong>At $${bankroll}:</strong> Full access to all timeframes and markets. Consider concentrating on 1h for best pair costs.`;
  }

  // Progression bar marker
  const maxBar = 2500;
  const pct = Math.min(100, (bankroll / maxBar) * 100);
  const marker = document.getElementById("progression-marker");
  marker.style.left = pct + "%";
  document.getElementById("progression-marker-label").textContent = "You: $" + bankroll.toLocaleString();

  // Next tier
  const nextSection = document.getElementById("next-tier-section");
  const nextTier = TIER_DEFS.find(t => bankroll < t.min);
  if (nextTier) {
    const needed = nextTier.min - bankroll;
    nextSection.innerHTML = `
      <div class="next-tier-header">Next Tier Unlock</div>
      <div class="next-tier-target">Reach <strong style="color:var(--green)">$${nextTier.min.toLocaleString()}</strong> (+$${needed.toLocaleString()} to go)</div>
      <ul class="unlock-list">
        <li>${nextTier.assets > tier.assets ? "+" + (nextTier.assets - tier.assets) + " more asset(s)" : "Same assets"}</li>
        <li>${nextTier.tfs.length > tier.tfs.length ? "Unlock " + nextTier.tfs.filter(t => !tier.tfs.includes(t)).join(", ") + " timeframe" : "Same timeframes"}</li>
        <li>${nextTier.concurrent} concurrent markets (from ${tier.concurrent})</li>
      </ul>
    `;
  } else {
    nextSection.innerHTML = `<div class="next-tier-header">Max tier reached</div>`;
  }

  // Sync asset option visual states
  ["btc","eth","sol","xrp"].forEach(a => {
    const opt = document.getElementById("asset-opt-" + a);
    const cb = document.getElementById("s_trade_" + a);
    if (opt && cb) {
      opt.classList.toggle("active", cb.checked);
    }
  });

  // Sync timeframe option visual states
  ["5m","15m","1h"].forEach(tf => {
    const opt = document.getElementById("tf-opt-" + tf);
    const cb = document.getElementById("s_trade_" + tf);
    if (opt && cb) {
      opt.classList.toggle("active", cb.checked && !opt.classList.contains("recommended"));
      // Keep recommended class for 1h if checked
      if (tf === "1h" && cb.checked) opt.classList.add("recommended");
      if (!cb.checked) {
        opt.classList.remove("active");
        opt.classList.remove("recommended");
      }
    }
  });
}

// Asset option click handlers
document.querySelectorAll(".asset-option").forEach(opt => {
  opt.addEventListener("click", () => {
    if (opt.classList.contains("disabled")) return;
    const cb = opt.querySelector("input[type=checkbox]");
    if (cb) {
      cb.checked = !cb.checked;
      opt.classList.toggle("active", cb.checked);
    }
  });
});

// Timeframe option click handlers
document.querySelectorAll(".tf-option").forEach(opt => {
  opt.addEventListener("click", () => {
    if (opt.classList.contains("disabled")) return;
    const cb = opt.querySelector("input[type=checkbox]");
    if (cb) {
      cb.checked = !cb.checked;
      const tf = opt.id.replace("tf-opt-", "");
      if (tf === "1h") {
        opt.classList.toggle("recommended", cb.checked);
        opt.classList.remove("active");
      } else {
        opt.classList.toggle("active", cb.checked);
        opt.classList.remove("recommended");
      }
    }
  });
});

// Update tier panel when bankroll changes
document.getElementById("s_bankroll").addEventListener("input", () => {
  const bankroll = Number(document.getElementById("s_bankroll").value) || 0;
  const settings = { bankroll };
  ["btc","eth","sol","xrp"].forEach(a => {
    settings["trade_" + a] = document.getElementById("s_trade_" + a)?.checked ?? false;
  });
  ["5m","15m","1h"].forEach(tf => {
    settings["trade_" + tf] = document.getElementById("s_trade_" + tf)?.checked ?? false;
  });
  settings.dry_run = document.getElementById("s_dry_run")?.checked ?? true;
  syncConfigUI(settings);
});

document.getElementById("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const body = {};
  form.querySelectorAll("input[name], select[name]").forEach(el => {
    if (el.type === "checkbox") body[el.name] = el.checked;
    else if (el.type === "number" || el.type === "range") body[el.name] = Number(el.value);
    else body[el.name] = el.value;
  });
  // Live mode: never let the form overwrite bankroll — on-chain USDC is source of truth.
  if (_lastStateSnapshot && _lastStateSnapshot.mode === "live") {
    delete body.bankroll;
  }
  try {
    const resp = await fetch(`${API_BASE}/api/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      showToast(`Save failed: HTTP ${resp.status}`, "error");
      return;
    }
    const data = await resp.json();
    if (data.ok) {
      showToast("Settings saved and applied.", "success");
      // Refresh state immediately so USDC balance updates
      try {
        const stateResp = await fetch(`${API_BASE}/api/state`);
        const stateData = await stateResp.json();
        applyStatus(stateData);
      } catch (_) {}
    } else {
      showToast("Save failed: " + (data.error || "unknown"), "error");
    }
  } catch (e) {
    showToast("Save failed: " + e, "error");
  }
});

// ────── Live bankroll field + Config status card ──────
// Cached most-recent state snapshot — updateConfigStatusCard can be called with
// no args (from cred-status refreshes) and still render consistently.
let _lastStateSnapshot = null;

function updateBankrollFieldForMode(snapshot) {
  const input = document.getElementById("s_bankroll");
  const btn = document.getElementById("btn-refresh-balance");
  const hint = document.getElementById("bankroll-hint");
  if (!input) return;
  const isLive = snapshot && snapshot.mode === "live";
  if (isLive) {
    const bal = Number(snapshot.usdc_balance) || 0;
    input.value = Math.round(bal * 100) / 100;
    input.disabled = true;
    input.readOnly = true;
    if (btn) btn.style.display = "";
    if (hint) {
      hint.textContent = "Live bankroll is your on-chain USDC balance — change it by funding/withdrawing from your wallet.";
    }
  } else {
    input.disabled = false;
    input.readOnly = false;
    if (btn) btn.style.display = "none";
    if (hint) {
      hint.textContent = "Ladder sizing, rungs, and position fractions are auto-calculated from this amount";
    }
  }
}

function _redactAddress(addr) {
  if (!addr || typeof addr !== "string") return "";
  if (addr.length < 10) return addr;
  return addr.slice(0, 6) + "..." + addr.slice(-4);
}

function _credsOverallState(credData) {
  // Return "valid" | "invalid" | "unverified" | "partial" | "none"
  if (!credData) return "none";
  const all = credData.has_private_key && credData.has_api_key
    && credData.has_api_secret && credData.has_api_passphrase;
  const any = credData.has_private_key || credData.has_api_key
    || credData.has_api_secret || credData.has_api_passphrase;
  const anyInvalid = Object.values(credValidationState).some(v => v === "invalid");
  const anyValid = Object.values(credValidationState).some(v => v === "valid");
  if (anyInvalid) return "invalid";
  if (all && anyValid) return "valid";
  if (all) return "unverified";  // present but never validated — don't claim valid
  if (any) return "partial";
  return "none";
}

function updateConfigStatusCard(snapshotArg) {
  if (snapshotArg) _lastStateSnapshot = snapshotArg;
  const snapshot = _lastStateSnapshot;
  const credData = window._lastCredData;

  const modeEl = document.getElementById("cfg-status-mode");
  const balEl = document.getElementById("cfg-status-balance");
  const walletEl = document.getElementById("cfg-status-wallet");
  const credsEl = document.getElementById("cfg-status-creds");

  if (modeEl) {
    if (!snapshot) {
      modeEl.textContent = "--";
      modeEl.className = "config-status-value";
    } else if (snapshot.mode === "live") {
      modeEl.textContent = "LIVE";
      modeEl.className = "config-status-value mode-live";
    } else {
      modeEl.textContent = "PAPER";
      modeEl.className = "config-status-value mode-paper";
    }
  }

  if (balEl) {
    const b = snapshot ? Number(snapshot.usdc_balance) || 0 : null;
    if (b != null) {
      balEl.textContent = "$" + b.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    } else {
      balEl.textContent = "--";
    }
  }

  if (walletEl) {
    if (snapshot && snapshot.mode === "live" && snapshot.wallet) {
      walletEl.textContent = _redactAddress(snapshot.wallet);
    } else {
      walletEl.textContent = "—";
    }
  }

  if (credsEl) {
    const state = _credsOverallState(credData);
    if (state === "valid") {
      credsEl.textContent = "\u2713 Valid";
      credsEl.className = "config-status-value creds-valid";
    } else if (state === "invalid") {
      credsEl.textContent = "\u2717 Invalid";
      credsEl.className = "config-status-value creds-invalid";
    } else if (state === "unverified") {
      credsEl.textContent = "Unverified";
      credsEl.className = "config-status-value creds-none";
    } else if (state === "partial") {
      credsEl.textContent = "Partial";
      credsEl.className = "config-status-value creds-none";
    } else {
      credsEl.textContent = "— Not configured";
      credsEl.className = "config-status-value creds-none";
    }
  }
}

// Refresh-balance button — re-queries on-chain balance. Only visible in live mode.
document.getElementById("btn-refresh-balance")?.addEventListener("click", async (e) => {
  const btn = e.target;
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Refreshing...";
  try {
    // /api/test-connection does a fresh live balance query.
    const resp = await fetch(`${API_BASE}/api/test-connection`, { method: "POST" });
    const data = await resp.json();
    if (data.ok && typeof data.balance === "number") {
      const input = document.getElementById("s_bankroll");
      if (input) input.value = data.balance;
      const statusBalEl = document.getElementById("cfg-status-balance");
      if (statusBalEl) {
        statusBalEl.textContent = "$" + data.balance.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      }
      showToast(`Balance refreshed: $${data.balance.toFixed(2)}`, "success");
    } else {
      showToast("Balance refresh failed: " + (data.error || "unknown"), "error");
    }
  } catch (err) {
    showToast("Balance refresh failed: " + err, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
});

// ────── Config (Credentials) Load/Save ──────
// credValidationState persists across ticks so the ✓/✗ checkmarks reflect the
// last validation result (not just "is this field non-empty in .env"). Cleared
// when user edits a field. Possible values per key: "valid" | "invalid" | null.
const credValidationState = {
  private_key: null,
  api_key: null,
  api_secret: null,
  api_passphrase: null,
};
let lastCredsBalance = null;  // $ balance from most recent successful validation
let lastCredsError = null;    // error string from most recent failed validation

function renderCredStatus(data) {
  const el = document.getElementById("cred-status");
  if (!el) return;
  const items = [
    { key: "private_key", label: "Private Key", present: data.has_private_key },
    { key: "api_key",     label: "API Key",     present: data.has_api_key },
    { key: "api_secret",  label: "API Secret",  present: data.has_api_secret },
    { key: "api_passphrase", label: "API Passphrase", present: data.has_api_passphrase },
  ];
  el.innerHTML = items.map(i => {
    const v = credValidationState[i.key];
    // Priority: validated invalid > validated valid > present > missing
    let cls, mark;
    if (v === "invalid") { cls = "cred-invalid"; mark = "\u2717"; }
    else if (v === "valid") { cls = "cred-ok"; mark = "\u2713"; }
    else if (i.present) { cls = "cred-ok"; mark = "\u2713"; }
    else { cls = "cred-missing"; mark = "\u2717"; }
    return `<span class="cred-item ${cls}">${mark} ${i.label}</span>`;
  }).join("");
}

async function loadCredStatus() {
  try {
    const resp = await fetch(`${API_BASE}/api/config`);
    const data = await resp.json();
    window._lastCredData = data;
    renderCredStatus(data);
    updateConfigStatusCard();
  } catch (e) {
    console.error("Load cred status failed:", e);
  }
}

// Clear validation state when user edits any credential field — the prior
// validation result no longer applies to the new value.
["private_key", "api_key", "api_secret", "api_passphrase"].forEach(k => {
  const el = document.getElementById(k);
  if (el) {
    el.addEventListener("input", () => {
      credValidationState[k] = null;
      renderCredStatus(window._lastCredData || {});
      updateConfigStatusCard();
    });
  }
});

document.getElementById("config-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const config = {
    private_key: document.getElementById("private_key").value.trim(),
    api_key: document.getElementById("api_key").value.trim(),
    api_secret: document.getElementById("api_secret").value.trim(),
    api_passphrase: document.getElementById("api_passphrase").value.trim(),
  };
  const submittedKeys = Object.keys(config).filter(k => config[k]);
  const submitBtn = e.target.querySelector("button[type=submit]");
  const origText = submitBtn ? submitBtn.textContent : null;
  if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = "Validating..."; }
  try {
    const resp = await fetch(`${API_BASE}/api/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
    const data = await resp.json();
    if (data.ok && data.saved) {
      // Saved + possibly validated.
      if (data.validated === true && typeof data.balance === "number") {
        showToast(`Credentials valid — wallet balance: $${data.balance.toFixed(2)}`, "success");
        lastCredsBalance = data.balance;
        lastCredsError = null;
        submittedKeys.forEach(k => { credValidationState[k] = "valid"; });
      } else {
        // Partial save (not all 4 creds present yet) — no validation performed.
        showToast("Credentials saved (validation skipped — full set not yet present).", "info");
      }
      loadCredStatus();
      document.getElementById("private_key").value = "";
      document.getElementById("api_key").value = "";
      document.getElementById("api_secret").value = "";
      document.getElementById("api_passphrase").value = "";
      if (data.restart_required) {
        const ok = await showModal(
          "Credentials saved. Restart the bot to switch to live trading? " +
          "This will cancel all paper orders and reinitialize with your live wallet."
        );
        if (ok) {
          triggerRestart();
        }
      }
    } else if (data.saved === false && data.error) {
      // Validation rejected the creds — server rolled back .env.
      showToast("Save failed: " + data.error, "error");
      lastCredsError = data.error;
      lastCredsBalance = null;
      submittedKeys.forEach(k => { credValidationState[k] = "invalid"; });
      loadCredStatus();
    } else if (data.ok) {
      // saved_any=false — nothing submitted, nothing to do.
      showToast("No changes to save.", "info");
    } else {
      showToast("Save failed: " + (data.error || "unknown"), "error");
    }
  } catch (e) {
    showToast("Save failed: " + e, "error");
  } finally {
    if (submitBtn && origText !== null) { submitBtn.disabled = false; submitBtn.textContent = origText; }
  }
});

// ────── Restart Bot ──────
async function triggerRestart() {
  showToast("Restarting bot...", "info");
  try {
    const resp = await fetch(`${API_BASE}/api/restart`, { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
      showToast("Bot is restarting. Reconnecting when it comes back up.", "warning");
      // Mark the UI as disconnected; the WS will auto-reconnect once the new
      // process binds the port (~3-5s).
      disconnectWebSocket();
      agentRunning = false;
      updateConnectionStatus(false);
      setTimeout(() => {
        agentRunning = true;
        connectWebSocket();
        startPolling();
      }, 4000);
    } else {
      showToast("Restart failed: " + (data.error || "unknown"), "error");
    }
  } catch (err) {
    // Errors during restart are expected (the server just exited).
    showToast("Restart requested. Reloading in a moment...", "info");
    setTimeout(() => window.location.reload(), 5000);
  }
}

document.getElementById("btn-restart")?.addEventListener("click", async () => {
  const ok = await showModal(
    "Restart the bot now? Resting orders will be cancelled gracefully and the bot will relaunch with the persisted config."
  );
  if (ok) triggerRestart();
});

// ────── Restart & Reset Paper ──────
// Shown only when configured mode is paper. Archives cumulative logs and
// reseeds bankroll from DRY_RUN_BANKROLL before delegating to /api/restart.
async function triggerRestartReset() {
  showToast("Archiving logs and restarting paper bot...", "info");
  try {
    const resp = await fetch(`${API_BASE}/api/restart-reset`, { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
      const n = (data.archived || []).length;
      showToast(`Paper reset — archived ${n} log(s), bankroll reseeded to $${data.bankroll_seed}.`, "warning");
      disconnectWebSocket();
      agentRunning = false;
      updateConnectionStatus(false);
      setTimeout(() => {
        agentRunning = true;
        connectWebSocket();
        startPolling();
      }, 4000);
    } else {
      showToast("Reset failed: " + (data.error || "unknown"), "error");
    }
  } catch (err) {
    showToast("Restart-reset requested. Reloading in a moment...", "info");
    setTimeout(() => window.location.reload(), 5000);
  }
}

document.getElementById("btn-restart-reset")?.addEventListener("click", async () => {
  const ok = await showModal(
    "Reset paper mode? This archives settlement + activity logs and reseeds bankroll to DRY_RUN_BANKROLL (default $10,000). Does NOT affect live mode."
  );
  if (ok) triggerRestartReset();
});

// Show the reset button only when configured_mode is paper. Runs on every
// state update so the toggle reflects .env changes made in the UI.
function updateRestartResetVisibility(snapshot) {
  const btn = document.getElementById("btn-restart-reset");
  const hint = document.getElementById("restart-reset-hint");
  if (!btn) return;
  const isPaper = (snapshot && snapshot.configured_mode) === "dry_run";
  btn.style.display = isPaper ? "" : "none";
  if (hint) hint.style.display = isPaper ? "" : "none";
}

document.getElementById("btn-restart-inline")?.addEventListener("click", async () => {
  const ok = await showModal(
    "Restart the bot now to apply the persisted config? Resting orders will be cancelled gracefully."
  );
  if (ok) triggerRestart();
});

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
    syncButtons(data.connected, data.cancel_only_mode);
    if (data.connected) {
      connectWebSocket();
      startPolling();
    }
  } catch (e) {
    console.error("Status check failed:", e);
  }
  loadSettings();
  loadCredStatus();
}

init();
