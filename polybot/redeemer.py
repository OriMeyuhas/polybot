"""On-chain token redemption after market settlement."""

import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class RedemptionEntry:
    condition_id: str
    token_ids: list[str]
    attempts: int = 0


class Redeemer:
    def __init__(self, max_retries: int = 10, backoff_sec: float = 2.0):
        self._max_retries = max_retries
        self._backoff_sec = backoff_sec
        self.pending: dict[str, RedemptionEntry] = {}
        self.failed: dict[str, RedemptionEntry] = {}

    def queue_redemption(self, condition_id: str, token_ids: list[str]) -> None:
        if condition_id not in self.pending and condition_id not in self.failed:
            self.pending[condition_id] = RedemptionEntry(
                condition_id=condition_id, token_ids=token_ids)
            log.info("Queued redemption for %s", condition_id)

    def _record_failure(self, condition_id: str) -> None:
        entry = self.pending.get(condition_id)
        if entry is None:
            return
        entry.attempts += 1
        if entry.attempts >= self._max_retries:
            self.failed[condition_id] = entry
            del self.pending[condition_id]
            log.error("Redemption failed after %d attempts for %s",
                      entry.attempts, condition_id)

    def _record_success(self, condition_id: str) -> None:
        self.pending.pop(condition_id, None)
        self.failed.pop(condition_id, None)
        log.info("Redemption succeeded for %s", condition_id)

    async def run(self, redeem_fn) -> None:
        """Main redemption loop. redeem_fn: async callable(cid, token_ids) -> float"""
        log.info("Redeemer started")
        while True:
            for cid in list(self.pending.keys()):
                entry = self.pending.get(cid)
                if entry is None:
                    continue
                try:
                    usdc_received = await redeem_fn(cid, entry.token_ids)
                    self._record_success(cid)
                    log.info("Redeemed %s: $%.2f USDC", cid, usdc_received)
                except Exception as exc:
                    log.warning("Redemption attempt %d failed for %s: %s",
                                entry.attempts + 1, cid, exc)
                    self._record_failure(cid)
                    backoff = self._backoff_sec * (2 ** entry.attempts)
                    backoff = min(backoff, 300.0)
                    await asyncio.sleep(backoff)
            await asyncio.sleep(10)
