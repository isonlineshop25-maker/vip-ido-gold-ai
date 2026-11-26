"""Paper-execution assumptions; illustrative, not observed broker charges."""

import os

DEFAULT_COSTS = (0.50, 0.10, 0.10)  # USD/oz per round: full spread, fees, adverse slippage on both legs


def read_costs():
    try:
        if any(name in os.environ for name in ("IDO_VALIDATION_SPREAD_USD_PER_OZ", "IDO_VALIDATION_FEES_USD_PER_OZ")):
            spread = float(os.environ["IDO_VALIDATION_SPREAD_USD_PER_OZ"])
            fees = float(os.environ["IDO_VALIDATION_FEES_USD_PER_OZ"])
        else:
            spread, fees, _ = DEFAULT_COSTS
        slip = float(os.environ.get("IDO_VALIDATION_SLIPPAGE_USD_PER_OZ", DEFAULT_COSTS[2]))
        if 0 < spread <= 100 and 0 <= fees <= 100 and 0 <= slip <= 100:
            return spread, fees, slip
    except (KeyError, ValueError):
        pass
    return None


COSTS = read_costs()
