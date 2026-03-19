(function() {
    const WS_URL = `ws://${location.host}/ws`;
    let ws = null;
    let reconnectTimer = null;

    function connect() {
        ws = new WebSocket(WS_URL);
        ws.onopen = () => {
            document.getElementById('disconnect-banner').style.display = 'none';
            if (reconnectTimer) { clearInterval(reconnectTimer); reconnectTimer = null; }
        };
        ws.onmessage = (e) => update(JSON.parse(e.data));
        ws.onclose = () => {
            document.getElementById('disconnect-banner').style.display = 'block';
            if (!reconnectTimer) reconnectTimer = setInterval(connect, 3000);
        };
        ws.onerror = () => ws.close();
    }

    function fmt(n, d=2) { return n != null ? n.toLocaleString(undefined, {minimumFractionDigits:d, maximumFractionDigits:d}) : '--'; }
    function fmtPct(ratio) { return (ratio * 100).toFixed(2) + '%'; }
    function fmtTime(sec) {
        if (sec <= 0) return '--';
        const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
        return `${m}:${s.toString().padStart(2,'0')}`;
    }
    function fmtUptime(sec) {
        const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60);
        return h > 0 ? `${h}h ${m}m` : `${m}m`;
    }
    function pnlClass(v) { return v >= 0 ? 'text-green' : 'text-red'; }
    function pairClass(v) { return v < 0.92 ? 'text-green' : v < 0.95 ? 'text-yellow' : 'text-red'; }
    function imbalClass(v) { return v < 0.30 ? 'text-green' : v < 0.60 ? 'text-yellow' : 'text-red'; }
    function tfLabel(sec) { return sec >= 3600 ? (sec/3600)+'h' : (sec/60)+'m'; }

    function update(d) {
        const badge = document.getElementById('mode-badge');
        badge.textContent = d.mode === 'dry_run' ? 'DRY RUN' : 'LIVE';
        badge.className = 'badge ' + (d.mode === 'dry_run' ? 'badge-dry' : 'badge-live');

        document.getElementById('cancel-badge').style.display = d.cancel_only_mode ? '' : 'none';
        document.getElementById('halted-badge').style.display = d.risk_halted ? '' : 'none';
        document.getElementById('uptime').textContent = fmtUptime(d.uptime_sec);
        document.getElementById('bankroll').textContent = '$' + fmt(d.bankroll);

        const pnlEl = document.getElementById('pnl');
        pnlEl.textContent = `$${d.daily_pnl >= 0 ? '+' : ''}${fmt(d.daily_pnl)} (${d.daily_pnl_pct >= 0 ? '+' : ''}${fmt(d.daily_pnl_pct)}%)`;
        pnlEl.className = 'stat-value ' + pnlClass(d.daily_pnl);

        const dot = document.getElementById('hb-dot');
        dot.className = 'dot ' + (d.heartbeat_healthy ? 'dot-green' : 'dot-red');

        document.getElementById('wallet-balance').textContent = '$' + fmt(d.wallet.usdc_balance);

        const spotsEl = document.getElementById('spots');
        spotsEl.innerHTML = '';
        for (const [asset, info] of Object.entries(d.spots)) {
            if (info.price <= 0) continue;
            const deltaStr = (info.delta >= 0 ? '+' : '') + fmtPct(info.delta);
            spotsEl.innerHTML += `<div class="spot-card">
                <div class="spot-asset">${asset}</div>
                <div class="spot-price">$${fmt(info.price)}</div>
                <div class="spot-delta ${pnlClass(info.delta)}">${deltaStr}</div>
            </div>`;
        }

        const lb = document.getElementById('ladders-body');
        lb.innerHTML = '';
        for (const l of d.ladders) {
            lb.innerHTML += `<tr>
                <td>${l.market_id.split('_').pop()}</td>
                <td>${tfLabel(l.timeframe_sec)}</td>
                <td>${l.up_resting}/${l.dn_resting}</td>
                <td>${fmt(l.up_filled,0)}/${fmt(l.dn_filled,0)}</td>
                <td>$${fmt(l.up_vwap,3)}</td>
                <td>$${fmt(l.dn_vwap,3)}</td>
                <td class="${pairClass(l.pair_cost)}">${fmt(l.pair_cost,3)}</td>
                <td class="${imbalClass(l.imbalance)}">${fmtPct(l.imbalance)}</td>
                <td>${fmtTime(l.time_left_sec)}</td>
            </tr>`;
        }
        if (!d.ladders.length) lb.innerHTML = '<tr><td colspan="9" class="text-muted">No active ladders</td></tr>';

        const pb = document.getElementById('positions-body');
        pb.innerHTML = '';
        for (const p of d.positions) {
            pb.innerHTML += `<tr>
                <td>${p.asset} ${p.market_id.split('_').pop()}</td>
                <td>${fmt(p.up_qty,1)}</td>
                <td>${fmt(p.dn_qty,1)}</td>
                <td class="${pnlClass(p.pnl_if_up)}">$${fmt(p.pnl_if_up)}</td>
                <td class="${pnlClass(p.pnl_if_down)}">$${fmt(p.pnl_if_down)}</td>
                <td class="${pnlClass(p.pnl_worst_case)}">$${fmt(p.pnl_worst_case)}</td>
            </tr>`;
        }
        if (!d.positions.length) pb.innerHTML = '<tr><td colspan="6" class="text-muted">No open positions</td></tr>';

        const w = d.wallet;
        document.getElementById('wallet-detail').innerHTML =
            `<b>${w.address}</b> &nbsp; Balance: $${fmt(w.usdc_balance)} &nbsp; Deployed: $${fmt(w.deployed)} &nbsp; Available: $${fmt(w.available)}`;

        const af = document.getElementById('activity-feed');
        af.innerHTML = '';
        for (const a of d.activity.slice().reverse()) {
            const ts = new Date(a.ts * 1000).toLocaleTimeString();
            const pnlStr = a.pnl != null ? `<span class="${pnlClass(a.pnl)}">$${a.pnl >= 0 ? '+' : ''}${fmt(a.pnl)}</span>` : '';
            af.innerHTML += `<div class="activity-row">
                <span class="activity-time">${ts}</span>
                <span class="activity-type">${a.type}</span>
                <span class="activity-asset">${a.asset}</span>
                <span class="activity-detail">${a.detail}</span>
                <span class="activity-pnl">${pnlStr}</span>
            </div>`;
        }
        if (!d.activity.length) af.innerHTML = '<div class="text-muted">No activity yet</div>';
    }

    fetch('/api/state').then(r => r.json()).then(update).catch(() => {});
    connect();
})();
