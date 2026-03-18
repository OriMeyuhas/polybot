def classify_strategy(
    slug: str, side: str, elapsed: int, total: int, spot_delta: float, asset: str,
    market_sides: dict[str, set[str]],
) -> str:
    """Classify the likely strategy behind a trade.

    Parameters
    ----------
    market_sides : dict[str, set[str]]
        Caller-owned mapping of slug -> set of sides seen. Mutated in place.
    """
    # Track market sides for spread detection
    if slug not in market_sides:
        market_sides[slug] = set()
    market_sides[slug].add(side.replace("EXIT_", ""))

    # Spread Capture: both UP and DOWN seen in same market
    if len(market_sides.get(slug, set())) >= 2:
        return "Spread Capture"

    # Latency Arb: late in window + significant spot movement
    if total > 0 and elapsed > 0:
        pct_elapsed = elapsed / total
        if pct_elapsed > 0.53 and abs(spot_delta) > 0.2:
            return "Latency Arb"

    # Pre-positioning: very early in window
    if total > 0 and elapsed > 0:
        pct_elapsed = elapsed / total
        if pct_elapsed < 0.15:
            return "Pre-positioning"

    # Exit: selling existing position
    if side.startswith("EXIT"):
        return "Exit"

    return "Directional"
