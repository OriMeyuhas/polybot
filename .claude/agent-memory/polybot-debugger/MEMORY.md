# PolyBot Debugger Memory

- [LOG_LEVEL=ERROR hides all guard diagnostics](feedback_log_level_visibility.md) — At ERROR level, INFO guards in ladder_manager.py are silent; always recommend LOG_LEVEL=INFO when investigating 0 ladder posts
- [Tightness filter threshold bug blocks all ladders](project_tightness_filter_bug.md) — best_ask_up + best_ask_dn > 0.98 fires on 100% of real binary markets; removed 2026-03-30
- [Zero-position expiries emit no Settled log](project_zero_position_no_settlement_log.md) — gate-miss + no fills = no position = no "Settled" line; this is by design, not a regression
