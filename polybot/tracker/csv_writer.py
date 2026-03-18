import csv
from datetime import datetime, timezone
from pathlib import Path


class TrackerCSVWriter:
    TRADE_COLUMNS = [
        "session_id", "timestamp", "tx_hash", "asset", "timeframe", "market_slug",
        "side", "outcome", "price", "size_usd", "size_shares",
        "spot_price_at_fill", "spot_1m_ago", "spot_3m_ago",
        "spot_delta_1m_pct", "spot_delta_3m_pct",
        "window_start_epoch", "window_end_epoch", "window_elapsed_sec",
        "window_total_sec", "window_pct_elapsed",
        "book_best_bid", "book_best_ask", "book_spread_pct",
        "strategy_guess",
    ]

    SETTLEMENT_COLUMNS = [
        "session_id", "timestamp", "market_slug", "asset", "timeframe",
        "window_start_epoch", "window_end_epoch",
        "settled_outcome", "settlement_price",
        "spot_at_open", "spot_at_close", "spot_change_pct",
        "whale_had_position", "whale_side", "whale_avg_price",
        "whale_total_usd", "whale_pnl_usd", "whale_roi_pct",
    ]

    SPOT_COLUMNS = [
        "session_id", "timestamp", "asset", "price", "price_1m_ago", "delta_1m_pct",
    ]

    BOOK_COLUMNS = [
        "session_id", "timestamp", "market_slug", "token_id", "side",
        "best_bid", "best_ask", "spread_pct", "mid_price",
        "depth_1c_bid", "depth_1c_ask", "depth_5c_bid", "depth_5c_ask",
        "depth_10c_bid", "depth_10c_ask", "num_bid_levels", "num_ask_levels",
    ]

    def __init__(self, data_dir: Path, session_id: str):
        data_dir.mkdir(parents=True, exist_ok=True)
        self._session_id = session_id
        self._date_str = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
        self._data_dir = data_dir
        self._files: dict[str, tuple] = {}  # name -> (file, writer)

    def _get_writer(self, name: str, columns: list[str]) -> csv.DictWriter:
        if name not in self._files:
            path = self._data_dir / f"{name}_{self._date_str}.csv"
            is_new = not path.exists()
            f = open(path, "a", newline="", encoding="utf-8")
            writer = csv.DictWriter(f, fieldnames=columns)
            if is_new:
                writer.writeheader()
                f.flush()
            self._files[name] = (f, writer)
        return self._files[name][1]

    def _write(self, name: str, columns: list[str], row: dict) -> None:
        row["session_id"] = self._session_id
        writer = self._get_writer(name, columns)
        writer.writerow(row)
        self._files[name][0].flush()

    def write_trade(self, row: dict) -> None:
        self._write("trades", self.TRADE_COLUMNS, row)

    def write_settlement(self, row: dict) -> None:
        self._write("settlements", self.SETTLEMENT_COLUMNS, row)

    def write_spot(self, row: dict) -> None:
        self._write("spots", self.SPOT_COLUMNS, row)

    def write_book(self, row: dict) -> None:
        self._write("book_snapshots", self.BOOK_COLUMNS, row)

    def close(self) -> None:
        for f, _ in self._files.values():
            f.close()
        self._files.clear()
