"""Sanitized connection evidence; never expose credentials or raw provider errors."""

import hmac
import json
import os

from spot import GOLD

def bridge_identity(token, account, operator_token, proxies):
    """Bind heartbeat evidence to the active authentication configuration."""
    if len(token) < 32 or len(operator_token) < 32 or token == operator_token or not account or not proxies:
        return None
    config = json.dumps(["ido-mt5-status-v1", account, operator_token, sorted(proxies)])
    return hmac.digest(token.encode(), config.encode(), "sha256").hex()


def bridge_record(db, identity):
    if not identity:
        return None
    return db.execute("SELECT seen,balance,currency FROM bridge WHERE id=1 AND identity=?", (identity,)).fetchone()


def bridge_check(row, identity, now):
    if not identity:
        return dict(status="not_configured", lastHeartbeatAt=None)
    if not row:
        return dict(status="waiting", lastHeartbeatAt=None)
    return dict(status="verified_demo" if 0 <= now - row["seen"] <= 90 else "stale", lastHeartbeatAt=row["seen"])


def deriv_configuration():
    # Reserve names for a later adapter. Presence is not an authentication test.
    checks = [bool(os.getenv(name, "").strip()) for name in ("IDO_DERIV_APP_ID", "IDO_DERIV_API_TOKEN")]
    return "configured_untested" if all(checks) else "partial" if any(checks) else "not_configured"


def connection_report(db, market, provider, identity, now):
    return dict(mode="demo_only", checkedAt=now, provider=provider, market=market.connection_check(now),
                spot=market.connection_check(now, GOLD),
                mt5Connection=bridge_check(bridge_record(db, identity), identity, now),
                deriv=deriv_configuration(), liveExecutionAllowed=False)
