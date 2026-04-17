#!/usr/bin/env python3
"""PolyBot entry point — launch the ladder market maker.

Usage:
    python run_bot.py

In DRY_RUN=true mode (default), no credentials are required — a paper CLOB
client simulates orders using real market data for fill simulation.

Requires PRIVATE_KEY, API_KEY, API_SECRET, API_PASSPHRASE in .env for live mode.
"""

import asyncio
import dataclasses
import logging
import os
import signal
import subprocess
import sys

# Force unbuffered output so logs appear immediately when piped
os.environ.setdefault("PYTHONUNBUFFERED", "1")

from polybot.config import load_bot_config
from polybot.bot import Bot
from polybot.web.server import create_app, start_gui_server


def _attempt_live_startup(cfg, log: logging.Logger) -> tuple[object, str]:
    """Try to bring up the bot in live mode. Degrade gracefully on failure.

    Returns (bot_instance, degraded_reason). `degraded_reason` is an empty
    string when live startup succeeded; otherwise it is a short human-readable
    explanation (e.g. "wallet_unfunded", "config_invalid", "credentials_invalid")
    that the dashboard will show as a banner. On degradation the returned bot
    runs with a paper CLOB client so the dashboard, data feeds, and mode toggle
    remain usable while the user fixes whatever is wrong.

    Strict behavior is preserved for the explicit "start trading" intent in
    `Bot.ui_start_full()` — that path still raises/blocks per existing rules.
    """
    # Missing credentials → can't even construct a live client. Degrade.
    missing = [
        name
        for name, val in (
            ("PRIVATE_KEY", cfg.private_key),
            ("API_KEY", cfg.api_key),
            ("API_SECRET", cfg.api_secret),
            ("API_PASSPHRASE", cfg.api_passphrase),
        )
        if not val
    ]
    if missing:
        reason = f"credentials_missing:{','.join(missing)}"
        log.warning(
            "LIVE mode requested but credentials missing (%s). "
            "Starting dashboard in degraded mode — paper client, live feeds, "
            "trading disabled. Fill creds and restart to go live.",
            ", ".join(missing),
        )
        degraded = dataclasses.replace(cfg, dry_run=True)
        bot = Bot(degraded)
        bot.mode = "live"  # keep display; degraded is surfaced via banner
        return bot, reason

    # Config out-of-bounds → would have been a sys.exit(1) before. Degrade.
    from polybot.config import validate_live_config
    errors = validate_live_config(cfg)
    if errors:
        for err in errors:
            log.warning("LIVE config rejected: %s", err)
        log.warning(
            "LIVE startup skipped due to %d config error(s). Starting dashboard "
            "in degraded mode — fix config and restart to go live.",
            len(errors),
        )
        degraded = dataclasses.replace(cfg, dry_run=True)
        bot = Bot(degraded)
        bot.mode = "live"
        reason = f"config_invalid:{errors[0]}"
        return bot, reason

    # Credentials & config look syntactically fine — try to build the Bot with
    # the real live client. This may still fail if creds are malformed (e.g.
    # non-hex private key) or Polymarket rejects them.
    try:
        bot = Bot(cfg)
        return bot, ""
    except Exception as exc:  # noqa: BLE001 — any failure is a degrade trigger
        log.warning(
            "LIVE Bot construction failed (%s: %s). Starting dashboard in "
            "degraded mode — paper client, live feeds, trading disabled.",
            type(exc).__name__,
            exc,
        )
        degraded = dataclasses.replace(cfg, dry_run=True)
        bot = Bot(degraded)
        bot.mode = "live"
        return bot, f"credentials_invalid:{type(exc).__name__}"


async def _restart_bot_process(
    stop_coro,
    argv: list[str] | None = None,
    *,
    _popen=None,
    _exit=None,
    _sleep=None,
    _logger: logging.Logger | None = None,
) -> None:
    """Graceful restart: stop the bot, spawn a replacement, then exit.

    Extracted from main() so tests can verify stop-then-spawn-then-exit
    ordering without actually killing the test process. Injected factories:

    - _popen: callable(argv, cwd, close_fds) -> Popen-like. Defaults to
      `subprocess.Popen`.
    - _exit: callable(status). Defaults to `os._exit` (ensures the asyncio
      loop doesn't get a chance to run finalizers that re-cancel orders).
    - _sleep: async callable(seconds). Defaults to `asyncio.sleep`. Exists so
      tests can skip the flush delay.

    stdout/stderr are inherited (close_fds=False) so a caller that launched
    us as `python run_bot.py > polybot.log 2>&1 &` keeps capturing output.
    """
    log = _logger or logging.getLogger("polybot")
    popen = _popen or subprocess.Popen
    exit_fn = _exit or os._exit
    sleep_fn = _sleep or asyncio.sleep

    log.info("Restart requested via /api/restart — shutting down...")
    try:
        await stop_coro()
    except Exception:
        log.exception("restart: bot.stop() raised")

    launch_argv = argv if argv is not None else [sys.executable, *sys.argv]
    child_env = dict(os.environ, POLYBOT_SKIP_CONFIRM="1")
    try:
        popen(launch_argv, cwd=os.getcwd(), close_fds=False, env=child_env)
        log.info("Spawned replacement process; exiting now.")
    except Exception:
        log.exception("restart: Popen failed")
        return

    await sleep_fn(0.5)
    exit_fn(0)


def main():
    cfg = load_bot_config()

    mode = "PAPER" if cfg.dry_run else "LIVE"

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format=f"%(asctime)s [{mode}] %(name)-20s %(levelname)-7s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("polybot.log", mode="w"),  # truncate on start
        ],
    )
    log = logging.getLogger("polybot")

    # Cycle 19: force DEBUG on the ladder_manager logger so the book_mid_gate
    # non-fire instrumentation lines reach polybot.log regardless of LOG_LEVEL.
    # Remove once the gate fires/non-fire distribution is understood.
    logging.getLogger("polybot.strategy.ladder_manager").setLevel(logging.DEBUG)

    log.info("Starting PolyBot in %s mode (bankroll=$%.2f)", mode, cfg.bankroll)

    degraded_reason = ""

    if not cfg.dry_run:
        if os.getenv("POLYBOT_SKIP_CONFIRM") == "1":
            log.info("LIVE MODE — CONFIRM skipped (launched via /api/restart)")
        else:
            print("\n!!  LIVE TRADING MODE — real orders will be placed!")
            print(f"   Bankroll: ${cfg.bankroll:,.2f}")
            confirm = input("   Type 'CONFIRM' to proceed: ")
            if confirm != "CONFIRM":
                print("Aborted.")
                sys.exit(0)
        bot, degraded_reason = _attempt_live_startup(cfg, log)
    else:
        bot = Bot(cfg)

    async def _restart_bot() -> None:
        await _restart_bot_process(bot.stop, _logger=log)

    if degraded_reason:
        # User-facing banner explaining why live mode couldn't engage. The
        # dashboard reads `stale_order_alert` and shows it at the top.
        banner_map = {
            "credentials_missing": "Live mode requested but credentials missing ({detail}). Dashboard is live; trading disabled. Fill creds and restart.",
            "credentials_invalid": "Live mode requested but credentials invalid ({detail}). Dashboard is live; trading disabled. Check credentials and restart.",
            "config_invalid": "Live mode requested but config out of safety bounds ({detail}). Dashboard is live; trading disabled. Fix config and restart.",
        }
        kind, _, detail = degraded_reason.partition(":")
        tpl = banner_map.get(kind, "Live mode degraded ({detail}). Dashboard is live; trading disabled.")
        bot.gui_state.update(
            stale_order_alert=tpl.format(detail=detail or kind),
            cancel_only_mode=True,
        )
        bot._cancel_only_mode = True
        bot._cancel_only_reason = kind

    app = create_app(
        state=bot.gui_state,
        start_fn=lambda: bot.ui_start_full(),
        stop_fn=bot.ui_stop,
        update_bankroll_fn=bot.position_manager.update_bankroll,
        restart_fn=_restart_bot,
    )

    async def run():
        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, shutdown_event.set)
        # On Windows, KeyboardInterrupt from Ctrl+C propagates as
        # CancelledError through asyncio.run() — no add_signal_handler
        # needed (and it is not supported).

        runner = await start_gui_server(app, cfg.web_port)
        log.info("Dashboard at http://127.0.0.1:%d", cfg.web_port)
        log.info("Press Start in the dashboard to begin trading")

        try:
            standby_task = asyncio.create_task(bot.run_standby())
            shutdown_waiter = asyncio.create_task(shutdown_event.wait())
            done, pending = await asyncio.wait(
                [standby_task, shutdown_waiter],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        except asyncio.CancelledError:
            pass
        finally:
            await bot.stop()
            await runner.cleanup()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Shutting down...")


if __name__ == "__main__":
    main()
