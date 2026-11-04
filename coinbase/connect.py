#!/usr/bin/env python3
"""Coinbase connection check (Advanced Trade API). Reads only: balances, permissions and prices. Places no orders.

  ../box-scanner/.venv/bin/python connect.py

Key: create a Coinbase Developer Platform "Secret API key" (signature algorithm ECDSA) with View and Trade
permissions and NOT Transfer, download its JSON file, and save it in this folder as cdp_api_key.json. That file is
gitignored, so it never leaves this computer. The check refuses to continue if the key can move money out.
"""
from __future__ import annotations

import sys
from pathlib import Path

KEY_FILE = Path(__file__).resolve().parent / "cdp_api_key.json"


def client():
    import urllib3.util.connection
    from coinbase.rest import RESTClient
    # the key's IP allowlist holds this Mac's IPv4 address; without this, requests may leave over IPv6 and get 401
    urllib3.util.connection.HAS_IPV6 = False
    if not KEY_FILE.exists():
        raise SystemExit(f"no key yet: save the downloaded Coinbase key file as {KEY_FILE}")
    c = RESTClient(key_file=str(KEY_FILE))
    perms = c.get_api_key_permissions()
    if getattr(perms, "can_transfer", False):
        raise SystemExit("this key can transfer money out of the account; create one without Transfer permission")
    return c, perms


def main() -> int:
    c, perms = client()
    print(f"connected: view={perms.can_view} trade={perms.can_trade} transfer={perms.can_transfer} "
          f"portfolio={getattr(perms, 'portfolio_type', '?')}")
    for a in c.get_accounts(limit=250).accounts:
        bal = float(a.available_balance["value"]) + float(a.hold["value"])
        if bal > 0:
            print(f"  {a.currency:<6} {bal:,.8f}")
    for p in ("BTC-USD", "ETH-USD", "SOL-USD"):
        print(f"  {p} price {float(c.get_product(p).price):,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
