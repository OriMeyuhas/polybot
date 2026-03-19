(function () {
  'use strict';

  /* ------------------------------------------------------------------ */
  /*  WebSocket                                                           */
  /* ------------------------------------------------------------------ */

  const WS_URL = `ws://${location.host}/ws`;
  let ws = null;
  let reconnectTimer = null;

  function connect() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
      return;
    }
    try {
      ws = new WebSocket(WS_URL);
    } catch (e) {
      showBanner(true);
      scheduleReconnect();
      return;
    }

    ws.onopen = function () {
      showBanner(false);
      if (reconnectTimer) {
        clearInterval(reconnectTimer);
        reconnectTimer = null;
      }
    };

    ws.onmessage = function (e) {
      try {
        update(JSON.parse(e.data));
      } catch (_) {}
    };

    ws.onclose = function () {
      showBanner(true);
      scheduleReconnect();
    };

    ws.onerror = function () {
      ws.close();
    };
  }

  function scheduleReconnect() {
    if (!reconnectTimer) {
      reconnectTimer = setInterval(function () {
        connect();
      }, 3000);
    }
  }

  function showBanner(visible) {
    var b = document.getElementById('disconnect-banner');
    if (b) b.style.display = visible ? 'block' : 'none';
  }

  /* ------------------------------------------------------------------ */
  /*  Controls                                                            */
  /* ------------------------------------------------------------------ */

  document.getElementById('btn-start').onclick = function () {
    fetch('/api/start', { method: 'POST' }).catch(function () {});
  };

  document.getElementById('btn-stop').onclick = function () {
    fetch('/api/stop', { method: 'POST' }).catch(function () {});
  };

  /* ------------------------------------------------------------------ */
  /*  Bankroll inline edit (dry run only)                                 */
  /* ------------------------------------------------------------------ */

  var _isDryRun = false;

  function setupBankrollEdit() {
    var el = document.getElementById('bankroll');
    if (!el) return;

    el.onclick = function () {
      if (!_isDryRun) return;
      if (el.querySelector('input')) return; // already editing

      var current = el.dataset.raw || '10000';
      el.textContent = '';

      var input = document.createElement('input');
      input.type = 'number';
      input.className = 'kpi-edit-input';
      input.value = current;
      input.min = '0';
      el.appendChild(input);
      input.focus();
      input.select();

      function commit() {
        var val = parseFloat(input.value);
        if (!isNaN(val) && val > 0) {
          fetch('/api/set-bankroll', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ bankroll: val }),
          }).catch(function () {});
        }
        // revert display (will be updated by next WS message)
        el.textContent = '$' + fmt(parseFloat(current));
        el.dataset.raw = current;
      }

      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { commit(); }
        if (e.key === 'Escape') {
          el.textContent = '$' + fmt(parseFloat(current));
          el.dataset.raw = current;
        }
      });

      input.addEventListener('blur', commit);
    };
  }

  setupBankrollEdit();

  /* ------------------------------------------------------------------ */
  /*  Formatters                                                          */
  /* ------------------------------------------------------------------ */

  function fmt(n, d) {
    if (d === undefined) d = 2;
    if (n == null || isNaN(n)) return '—';
    return n.toLocaleString(undefined, {
      minimumFractionDigits: d,
      maximumFractionDigits: d,
    });
  }

  function fmtPct(ratio) {
    if (ratio == null || isNaN(ratio)) return '—';
    return (ratio * 100).toFixed(2) + '%';
  }

  function fmtTime(sec) {
    if (sec == null || sec <= 0) return '—';
    var m = Math.floor(sec / 60);
    var s = Math.floor(sec % 60);
    return m + ':' + s.toString().padStart(2, '0');
  }

  function fmtUptime(sec) {
    if (sec == null || isNaN(sec)) return '—';
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    return h > 0 ? h + 'h ' + m + 'm' : m + 'm';
  }

  function tfLabel(sec) {
    if (!sec) return '—';
    if (sec >= 3600) return (sec / 3600) + 'h';
    return (sec / 60) + 'm';
  }

  function pnlClass(v) {
    return v >= 0 ? 'text-green' : 'text-red';
  }

  function pairClass(v) {
    if (v < 0.92) return 'text-green';
    if (v < 0.95) return 'text-yellow';
    return 'text-red';
  }

  function imbalClass(v) {
    if (v < 0.30) return 'text-green';
    if (v < 0.60) return 'text-yellow';
    return 'text-red';
  }

  function activityTypeClass(type) {
    switch ((type || '').toUpperCase()) {
      case 'FILL':   return 'type-fill';
      case 'LADDER': return 'type-ladder';
      case 'SETTLE': return 'type-settle';
      case 'CANCEL': return 'type-cancel';
      default:       return 'type-other';
    }
  }

  function esc(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* ------------------------------------------------------------------ */
  /*  Main update function                                                */
  /* ------------------------------------------------------------------ */

  function update(d) {
    if (!d) return;

    /* ---- Mode badge ---- */
    var badge = document.getElementById('mode-badge');
    if (badge) {
      var isDry = d.mode === 'dry_run';
      _isDryRun = isDry;
      badge.textContent = isDry ? 'DRY RUN' : 'LIVE';
      badge.className = 'badge ' + (isDry ? 'badge-dry' : 'badge-live');
    }

    /* ---- Cancel / halted badges ---- */
    var cancelBadge = document.getElementById('cancel-badge');
    if (cancelBadge) cancelBadge.style.display = d.cancel_only_mode ? '' : 'none';

    var haltedBadge = document.getElementById('halted-badge');
    if (haltedBadge) haltedBadge.style.display = d.risk_halted ? '' : 'none';

    /* ---- Uptime ---- */
    var uptimeEl = document.getElementById('uptime');
    if (uptimeEl) uptimeEl.textContent = fmtUptime(d.uptime_sec);

    /* ---- Heartbeat dot ---- */
    var dot = document.getElementById('hb-dot');
    if (dot) dot.className = 'dot ' + (d.heartbeat_healthy ? 'dot-green' : 'dot-red');

    /* ---- Start / Stop buttons ----
       Show btn-stop when running (!cancel_only_mode),
       show btn-start when stopped (cancel_only_mode means no new orders = stopped) */
    var btnStart = document.getElementById('btn-start');
    var btnStop  = document.getElementById('btn-stop');
    var isRunning = !d.cancel_only_mode;
    if (btnStart) btnStart.style.display = isRunning ? 'none' : '';
    if (btnStop)  btnStop.style.display  = isRunning ? '' : 'none';

    /* ---- KPI: Bankroll ---- */
    var bankrollEl = document.getElementById('bankroll');
    if (bankrollEl) {
      bankrollEl.dataset.raw = d.bankroll != null ? String(d.bankroll) : '0';
      if (!bankrollEl.querySelector('input')) {
        // Not currently being edited
        bankrollEl.textContent = '$' + fmt(d.bankroll);
        if (_isDryRun) {
          bankrollEl.classList.add('clickable');
        } else {
          bankrollEl.classList.remove('clickable');
        }
      }
    }

    /* ---- KPI: PnL ---- */
    var pnlEl = document.getElementById('pnl');
    if (pnlEl) {
      var sign = d.daily_pnl >= 0 ? '+' : '';
      var pctSign = d.daily_pnl_pct >= 0 ? '+' : '';
      pnlEl.textContent =
        '$' + sign + fmt(d.daily_pnl) +
        ' (' + pctSign + (d.daily_pnl_pct != null ? d.daily_pnl_pct.toFixed(2) : '0.00') + '%)';
      pnlEl.className = 'kpi-value ' + pnlClass(d.daily_pnl);
    }

    /* ---- KPI: Deployed ---- */
    var deployedEl = document.getElementById('kpi-deployed');
    if (deployedEl && d.wallet) {
      var deployed = (d.wallet.on_orders || 0) + (d.wallet.in_positions || 0);
      var deployedPct = d.bankroll > 0 ? (deployed / d.bankroll) : 0;
      deployedEl.textContent = '$' + fmt(deployed) + ' (' + fmtPct(deployedPct) + ')';
    }

    /* ---- KPI: Available ---- */
    var availEl = document.getElementById('kpi-available');
    if (availEl && d.wallet) {
      availEl.textContent = '$' + fmt(d.wallet.available);
    }

    /* ---- KPI: Trades / Positions ---- */
    var tradesEl = document.getElementById('kpi-trades');
    if (tradesEl) {
      var posCount = d.positions ? d.positions.length : 0;
      tradesEl.textContent =
        (d.trade_count != null ? d.trade_count : '—') +
        ' (' + posCount + ' pos)';
    }

    /* ---- Spots ---- */
    renderSpots(d.spots || {});

    /* ---- Ladders ---- */
    renderLadders(d.ladders || []);

    /* ---- Positions ---- */
    renderPositions(d.positions || []);

    /* ---- Wallet ---- */
    renderWallet(d.wallet, d.mode);

    /* ---- Activity ---- */
    renderActivity(d.activity || []);
  }

  /* ------------------------------------------------------------------ */
  /*  Spots                                                               */
  /* ------------------------------------------------------------------ */

  function renderSpots(spots) {
    var el = document.getElementById('spots');
    if (!el) return;

    var html = '';
    var entries = Object.entries(spots);
    for (var i = 0; i < entries.length; i++) {
      var asset = entries[i][0];
      var info  = entries[i][1];
      if (!info || info.price <= 0) continue;
      var deltaSign = info.delta >= 0 ? '+' : '';
      var deltaStr  = deltaSign + fmtPct(info.delta);
      var deltaClass = pnlClass(info.delta);
      html +=
        '<div class="spot-card">' +
          '<div class="spot-asset">' + esc(asset) + '</div>' +
          '<div class="spot-price">$' + fmt(info.price, 2) + '</div>' +
          '<div class="spot-delta ' + deltaClass + '">' + esc(deltaStr) + '</div>' +
        '</div>';
    }
    el.innerHTML = html;
  }

  /* ------------------------------------------------------------------ */
  /*  Ladders                                                             */
  /* ------------------------------------------------------------------ */

  function renderLadders(ladders) {
    var tbody = document.getElementById('ladders-body');
    if (!tbody) return;

    if (!ladders.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="9">No active ladders</td></tr>';
      return;
    }

    var html = '';
    for (var i = 0; i < ladders.length; i++) {
      var l = ladders[i];
      var marketText = esc(l.asset) + ' ' + esc(l.market_id.split('_').pop());
      var marketLabel = l.condition_id && l.condition_id.startsWith('0x') && l.condition_id.length > 10
        ? '<a href="https://polymarket.com/event/' + esc(l.condition_id) + '" target="_blank" class="market-link">' + marketText + '</a>'
        : marketText;
      var tf          = esc(tfLabel(l.timeframe_sec));
      var askUp       = '<span class="text-green">$' + fmt(l.ask_up, 2) + '</span>';
      var askDn       = '<span class="text-red">$'   + fmt(l.ask_dn, 2) + '</span>';
      var fillUp      = esc(l.up_filled_count) + '/' + esc(l.up_total_rungs);
      var fillDn      = esc(l.dn_filled_count) + '/' + esc(l.dn_total_rungs);
      var pairCls     = pairClass(l.pair_cost);
      var pairVal     = '<span class="' + pairCls + '">' + fmt(l.pair_cost, 3) + '</span>';
      var imbalCls    = imbalClass(l.imbalance);
      var imbalVal    = '<span class="' + imbalCls + '">' + fmtPct(l.imbalance) + '</span>';
      var timeVal     = esc(fmtTime(l.time_left_sec));

      html +=
        '<tr>' +
          '<td>' + marketLabel + '</td>' +
          '<td>' + tf + '</td>' +
          '<td>' + askUp + '</td>' +
          '<td>' + askDn + '</td>' +
          '<td>' + fillUp + '</td>' +
          '<td>' + fillDn + '</td>' +
          '<td>' + pairVal + '</td>' +
          '<td>' + imbalVal + '</td>' +
          '<td>' + timeVal + '</td>' +
        '</tr>';
    }
    tbody.innerHTML = html;
  }

  /* ------------------------------------------------------------------ */
  /*  Positions                                                           */
  /* ------------------------------------------------------------------ */

  function renderPositions(positions) {
    var tbody = document.getElementById('positions-body');
    if (!tbody) return;

    if (!positions.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No open positions</td></tr>';
      return;
    }

    var html = '';
    for (var i = 0; i < positions.length; i++) {
      var p = positions[i];
      var market = esc(p.asset) + ' ' + esc(tfLabel(p.timeframe_sec));
      var qtyUp  = fmt(p.up_qty, 1);
      var qtyDn  = fmt(p.dn_qty, 1);
      var ifUp   = '<span class="' + pnlClass(p.pnl_if_up)   + '">$' + fmt(p.pnl_if_up) + '</span>';
      var ifDn   = '<span class="' + pnlClass(p.pnl_if_down) + '">$' + fmt(p.pnl_if_down) + '</span>';
      var worst  = '<span class="' + pnlClass(p.pnl_worst_case) + '">$' + fmt(p.pnl_worst_case) + '</span>';

      html +=
        '<tr>' +
          '<td>' + market + '</td>' +
          '<td>' + qtyUp + '</td>' +
          '<td>' + qtyDn + '</td>' +
          '<td>' + ifUp  + '</td>' +
          '<td>' + ifDn  + '</td>' +
          '<td>' + worst + '</td>' +
        '</tr>';
    }
    tbody.innerHTML = html;
  }

  /* ------------------------------------------------------------------ */
  /*  Wallet                                                              */
  /* ------------------------------------------------------------------ */

  function renderWallet(w, mode) {
    if (!w) return;

    var addrEl   = document.getElementById('wallet-address');
    var balEl    = document.getElementById('wallet-balance');
    var ordersEl = document.getElementById('wallet-on-orders');
    var posEl    = document.getElementById('wallet-in-positions');
    var availEl  = document.getElementById('wallet-available');

    if (addrEl) addrEl.textContent = w.address || '—';

    if (balEl) {
      balEl.textContent = '$' + fmt(w.usdc_balance);
      if (mode === 'dry_run') {
        balEl.classList.add('clickable');
        balEl.onclick = function () {
          // Clicking wallet balance in dry run also triggers bankroll edit
          var bankrollEl = document.getElementById('bankroll');
          if (bankrollEl) bankrollEl.click();
        };
      } else {
        balEl.classList.remove('clickable');
        balEl.onclick = null;
      }
    }

    if (ordersEl)  ordersEl.textContent  = '$' + fmt(w.on_orders);
    if (posEl)     posEl.textContent     = '$' + fmt(w.in_positions);
    if (availEl)   availEl.textContent   = '$' + fmt(w.available);
  }

  /* ------------------------------------------------------------------ */
  /*  Activity                                                            */
  /* ------------------------------------------------------------------ */

  function renderActivity(activity) {
    var list = document.getElementById('activity-list');
    if (!list) return;

    if (!activity.length) {
      list.innerHTML = '<div class="empty-row">No activity yet</div>';
      return;
    }

    var sorted = activity.slice().reverse();
    var html = '';
    for (var i = 0; i < sorted.length; i++) {
      var a  = sorted[i];
      var ts = new Date(a.ts * 1000).toLocaleTimeString();
      var typeCls = activityTypeClass(a.type);
      var pnlStr  = '';
      if (a.pnl != null) {
        var pnlSign = a.pnl >= 0 ? '+' : '';
        pnlStr = '<span class="' + pnlClass(a.pnl) + '">$' + pnlSign + fmt(a.pnl) + '</span>';
      }
      html +=
        '<div class="activity-row">' +
          '<span class="activity-time">'   + esc(ts)     + '</span>' +
          '<span class="activity-type '    + typeCls + '">' + esc(a.type)   + '</span>' +
          '<span class="activity-asset">'  + esc(a.asset) + '</span>' +
          '<span class="activity-detail">' + esc(a.detail) + '</span>' +
          '<span class="activity-pnl">'    + pnlStr       + '</span>' +
        '</div>';
    }
    list.innerHTML = html;
  }

  /* ------------------------------------------------------------------ */
  /*  Bootstrap                                                           */
  /* ------------------------------------------------------------------ */

  fetch('/api/state')
    .then(function (r) { return r.json(); })
    .then(update)
    .catch(function () {});

  connect();

})();
