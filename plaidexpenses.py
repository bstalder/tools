#!/usr/bin/env python3
"""Personal expense tracker built on the Plaid API (stdlib only, no pip installs).

Commands:
  check     look up Plaid coverage + OAuth status for named institutions
  link      connect a bank/card account via Plaid Hosted Link in your browser
  accounts  list linked institutions, accounts, and current balances
  sync      pull new/changed transactions: Plaid items, Coinbase, wallets
  report    monthly spending by category vs. budgets.json
  csv       dump stored transactions as CSV to stdout
  crypto    portfolio snapshot: Coinbase balances + watch-only wallets
  history   on-chain transaction history for the watch-only wallets

Configuration (environment):
  PLAID_CLIENT_ID   your Plaid client id (dashboard.plaid.com -> Keys)
  PLAID_SECRET      the secret for the chosen environment; an "op://..."
                    1Password secret reference is resolved via `op read`
  PLAID_ENV         sandbox (default) or production

State lives in ~/.local/share/plaid-expenses/ (created chmod 700):
  items.json         access tokens + sync cursors  (chmod 600 -- never commit)
  transactions.json  local transaction store
  budgets.json       {"CATEGORY_NAME": monthly_dollar_limit, ...}
  crypto.json        Coinbase CDP key ref + watch-only wallet addresses
"""

import argparse
import base64
import csv
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("PLAID_EXPENSES_DIR",
                               Path.home() / ".local/share/plaid-expenses"))
ITEMS_FILE = DATA_DIR / "items.json"
TX_FILE = DATA_DIR / "transactions.json"
BUDGETS_FILE = DATA_DIR / "budgets.json"
LINK_TIMEOUT_SECONDS = 1800

PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}


# ---------------------------------------------------------------- plumbing

def plaid_env() -> str:
    env = os.environ.get("PLAID_ENV", "sandbox")
    if env not in PLAID_HOSTS:
        sys.exit(f"PLAID_ENV must be one of {list(PLAID_HOSTS)}, got {env!r}")
    return env


def credentials() -> tuple[str, str]:
    client_id = os.environ.get("PLAID_CLIENT_ID")
    secret = os.environ.get("PLAID_SECRET")
    if not client_id or not secret:
        sys.exit("Set PLAID_CLIENT_ID and PLAID_SECRET (see README).")
    if secret.startswith("op://"):
        try:
            secret = subprocess.run(
                ["op", "read", secret], capture_output=True, text=True,
                check=True).stdout.strip()
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            sys.exit(f"Could not resolve PLAID_SECRET via 1Password CLI: {e}")
    return client_id, secret


def plaid_post(endpoint: str, payload: dict) -> dict:
    client_id, secret = credentials()
    body = {"client_id": client_id, "secret": secret, **payload}
    req = urllib.request.Request(
        PLAID_HOSTS[plaid_env()] + endpoint,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("error_message", detail)
        except json.JSONDecodeError:
            pass
        sys.exit(f"Plaid {endpoint} failed ({e.code}): {detail}")


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data, private: bool = False) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.chmod(0o700)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    if private:
        path.chmod(0o600)


# ---------------------------------------------------------------- check

def cmd_check(args) -> None:
    print(f"Plaid coverage ({plaid_env()} directory):\n")
    for name in args.banks:
        resp = plaid_post("/institutions/search", {
            "query": name,
            "products": ["transactions"],
            "country_codes": ["US"],
        })
        hits = resp.get("institutions", [])
        if not hits:
            print(f"{name:<24} NOT FOUND with transactions support")
            continue
        for i, inst in enumerate(hits[:2]):
            label = name if i == 0 else ""
            oauth = "oauth" if inst["oauth"] else "credentials"
            print(f"{label:<24} {inst['name']:<44} {oauth:<12}"
                  f" {inst['institution_id']}")


# ---------------------------------------------------------------- link

def save_item(public_token: str) -> str:
    exch = plaid_post("/item/public_token/exchange",
                      {"public_token": public_token})
    institution = "unknown"
    inst_id = plaid_post("/item/get", {
        "access_token": exch["access_token"]})["item"].get("institution_id")
    if inst_id:
        institution = plaid_post("/institutions/get_by_id", {
            "institution_id": inst_id,
            "country_codes": ["US"]})["institution"]["name"]
    items = load_json(ITEMS_FILE, {"items": []})
    items["items"].append({
        "item_id": exch["item_id"],
        "access_token": exch["access_token"],
        "institution": institution,
        "cursor": "",
    })
    save_json(ITEMS_FILE, items, private=True)
    return institution


def cmd_link(_args) -> None:
    resp = plaid_post("/link/token/create", {
        "client_name": "plaid-expenses",
        "user": {"client_user_id": "plaid-expenses-local"},
        "products": ["transactions"],
        "country_codes": ["US"],
        "language": "en",
        "transactions": {"days_requested": 730},
        # Hosted Link: Plaid hosts the flow and handles OAuth bank redirects,
        # so no redirect URI needs to be registered and no local server runs.
        "hosted_link": {"url_lifetime_seconds": LINK_TIMEOUT_SECONDS},
    })
    url = resp["hosted_link_url"]
    print(f"Complete the link flow in your browser:\n\n  {url}\n")
    print("(Credentials are entered on Plaid's hosted page; OAuth banks like"
          " Chase or\n Schwab bounce you to their own login. Waiting for"
          " completion...)")
    webbrowser.open(url)
    deadline = time.monotonic() + LINK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(5)
        status = plaid_post("/link/token/get", {"link_token": resp["link_token"]})
        for session in status.get("link_sessions") or []:
            results = (session.get("results") or {}).get("item_add_results") or []
            if results:
                for added in results:
                    print(f"Linked: {save_item(added['public_token'])}")
                print("Run `plaidexpenses.py sync` to pull transactions.")
                return
            if session.get("finished_at"):
                sys.exit("Link session finished without adding an account.")
    sys.exit(f"Timed out after {LINK_TIMEOUT_SECONDS // 60} minutes waiting"
             " for Link to complete.")


# ---------------------------------------------------------------- accounts

def cmd_accounts(_args) -> None:
    items = load_json(ITEMS_FILE, {"items": []})["items"]
    if not items:
        sys.exit("No linked accounts. Run `plaidexpenses.py link` first.")
    for item in items:
        resp = plaid_post("/accounts/get", {"access_token": item["access_token"]})
        print(f"\n{item['institution']}")
        for acct in resp["accounts"]:
            bal = acct["balances"]["current"]
            cur = acct["balances"].get("iso_currency_code") or ""
            print(f"  {acct['name']:<30} {acct['subtype'] or acct['type']:<12}"
                  f" {bal:>12,.2f} {cur}")


# ---------------------------------------------------------------- sync

def slim(tx: dict) -> dict:
    pfc = tx.get("personal_finance_category") or {}
    legacy = tx.get("category") or []
    return {
        "date": tx["date"],
        "name": tx.get("merchant_name") or tx["name"],
        "amount": tx["amount"],  # Plaid: positive = money out
        "currency": tx.get("iso_currency_code"),
        "category": pfc.get("primary") or (legacy[0].upper().replace(" ", "_")
                                           if legacy else "UNCATEGORIZED"),
        "account_id": tx["account_id"],
        "pending": tx.get("pending", False),
    }


def cmd_sync(_args) -> None:
    items = load_json(ITEMS_FILE, {"items": []})
    if not items["items"] and not CRYPTO_FILE.exists():
        sys.exit("Nothing to sync -- run `plaidexpenses.py link` (banks)"
                 " or `plaidexpenses.py crypto` (wallets/Coinbase) first.")
    store = load_json(TX_FILE, {})
    for item in items["items"]:
        added = modified = removed = 0
        while True:
            resp = plaid_post("/transactions/sync", {
                "access_token": item["access_token"],
                "cursor": item["cursor"],
                "count": 500,
            })
            for tx in resp["added"]:
                store[tx["transaction_id"]] = slim(tx)
                added += 1
            for tx in resp["modified"]:
                store[tx["transaction_id"]] = slim(tx)
                modified += 1
            for tx in resp["removed"]:
                if store.pop(tx["transaction_id"], None):
                    removed += 1
            item["cursor"] = resp["next_cursor"]
            if not resp["has_more"]:
                break
        print(f"{item['institution']}: +{added} added, {modified} modified,"
              f" -{removed} removed")
    if CRYPTO_FILE.exists():
        sync_crypto(store)
    save_json(TX_FILE, store)
    save_json(ITEMS_FILE, items, private=True)
    print(f"Store now holds {len(store)} transactions.")


# ---------------------------------------------------------------- report

DEFAULT_BUDGETS = {
    "_comment": "Monthly limits in dollars, keyed by Plaid category "
                "(run `report` to see category names in your data). "
                "Categories not listed here are shown but not budgeted.",
    "FOOD_AND_DRINK": 600,
    "GENERAL_MERCHANDISE": 300,
    "TRANSPORTATION": 200,
}


def cmd_report(args) -> None:
    month = args.month or date.today().strftime("%Y-%m")
    store = load_json(TX_FILE, {})
    if not store:
        sys.exit("No transactions stored. Run `plaidexpenses.py sync` first.")
    if not BUDGETS_FILE.exists():
        save_json(BUDGETS_FILE, DEFAULT_BUDGETS)
        print(f"Created budget template at {BUDGETS_FILE} -- edit it to set"
              " your limits.\n")
    budgets = {k: v for k, v in load_json(BUDGETS_FILE, {}).items()
               if not k.startswith("_")}

    spend: dict[str, float] = defaultdict(float)
    income = 0.0
    for tx in store.values():
        if not tx["date"].startswith(month):
            continue
        if tx["amount"] > 0:
            spend[tx["category"]] += tx["amount"]
        else:
            income += -tx["amount"]

    if not spend and not income:
        sys.exit(f"No transactions found for {month}.")

    print(f"Spending report for {month}\n")
    print(f"{'category':<28} {'spent':>10} {'budget':>10} {'left':>10}")
    print("-" * 62)
    total = 0.0
    for cat in sorted(spend, key=spend.get, reverse=True):
        amount = spend[cat]
        total += amount
        limit = budgets.get(cat)
        if limit is not None:
            left = limit - amount
            flag = "  ** OVER **" if left < 0 else ""
            print(f"{cat:<28} {amount:>10,.2f} {limit:>10,.2f}"
                  f" {left:>10,.2f}{flag}")
        else:
            print(f"{cat:<28} {amount:>10,.2f} {'-':>10} {'-':>10}")
    print("-" * 62)
    print(f"{'total spent':<28} {total:>10,.2f}")
    print(f"{'total income/refunds':<28} {income:>10,.2f}")
    print(f"{'net':<28} {income - total:>10,.2f}")


# ---------------------------------------------------------------- crypto

CRYPTO_FILE = DATA_DIR / "crypto.json"
CRYPTO_TEMPLATE = {
    "_comment": "coinbase_key: path to the CDP API key JSON downloaded from"
                " portal.cdp.coinbase.com (create it read-only, ES256 format),"
                " or an op:// 1Password reference to that JSON's contents."
                " wallets: watch-only public addresses -- never put private"
                " keys or seed phrases anywhere near this file.",
    "coinbase_key": "",
    "eth_rpc": "https://ethereum-rpc.publicnode.com",
    "wallets": [
        {"label": "example-ledger", "chain": "btc", "address": "bc1q..."},
        {"label": "example-metamask", "chain": "eth", "address": "0x..."},
    ],
}


def http_json(url: str, payload: dict = None, headers: dict = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Content-Type": "application/json",
                 "User-Agent": "plaid-expenses/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def der_sig_to_raw(der: bytes) -> bytes:
    """Convert openssl's DER ECDSA signature to the raw 64-byte r||s JWTs use."""
    def read_int(i: int) -> tuple[int, int]:
        if der[i] != 0x02:
            raise ValueError("unexpected DER structure in ECDSA signature")
        n = der[i + 1]
        return int.from_bytes(der[i + 2:i + 2 + n], "big"), i + 2 + n
    if der[0] != 0x30:
        raise ValueError("not a DER sequence")
    r, i = read_int(2)
    s, _ = read_int(i)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def coinbase_jwt(key_name: str, key_pem: str, method: str, path: str) -> str:
    """Build the ES256 JWT the Coinbase App API requires, signing via openssl
    so the EC math stays in battle-tested code rather than hand-rolled Python."""
    now = int(time.time())
    header = {"alg": "ES256", "kid": key_name, "typ": "JWT",
              "nonce": secrets.token_hex(16)}
    claims = {"iss": "cdp", "sub": key_name, "nbf": now, "exp": now + 120,
              "uri": f"{method} api.coinbase.com{path}"}
    signing_input = (b64url(json.dumps(header).encode()) + "."
                     + b64url(json.dumps(claims).encode()))
    fd, pem_file = tempfile.mkstemp(suffix=".pem")
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, key_pem.encode())
        os.close(fd)
        der = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", pem_file],
            input=signing_input.encode(), capture_output=True,
            check=True).stdout
    finally:
        os.unlink(pem_file)
    return signing_input + "." + b64url(der_sig_to_raw(der))


def coinbase_key(cfg: dict):
    ref = cfg.get("coinbase_key", "")
    if not ref:
        return None
    if ref.startswith("op://"):
        raw = subprocess.run(["op", "read", ref], capture_output=True,
                             text=True, check=True).stdout
    else:
        raw = Path(ref).expanduser().read_text()
    key = json.loads(raw)
    return key["name"], key["privateKey"]


def coinbase_paged(key: tuple, path: str):
    while path:
        token = coinbase_jwt(key[0], key[1], "GET", path.split("?")[0])
        resp = http_json("https://api.coinbase.com" + path,
                         headers={"Authorization": f"Bearer {token}"})
        yield from resp["data"]
        path = (resp.get("pagination") or {}).get("next_uri")


def coinbase_holdings(cfg: dict) -> list:
    key = coinbase_key(cfg)
    if not key:
        return []
    return [("coinbase", a["balance"]["currency"], float(a["balance"]["amount"]))
            for a in coinbase_paged(key, "/v2/accounts?limit=100")
            if float(a["balance"]["amount"])]


def wallet_holding(wallet: dict, cfg: dict) -> tuple:
    chain = wallet["chain"].lower()
    if chain == "btc":
        stats = http_json("https://mempool.space/api/address/"
                          + wallet["address"])["chain_stats"]
        sats = stats["funded_txo_sum"] - stats["spent_txo_sum"]
        return (wallet.get("label", "wallet"), "BTC", sats / 1e8)
    if chain == "eth":
        resp = http_json(cfg.get("eth_rpc", "https://ethereum-rpc.publicnode.com"),
                         {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                          "params": [wallet["address"], "latest"]})
        return (wallet.get("label", "wallet"), "ETH",
                int(resp["result"], 16) / 1e18)
    raise ValueError(f"unsupported chain {chain!r} (btc and eth supported;"
                     " ERC-20 tokens on-chain are not tracked)")


def btc_history(addr: str, limit: int) -> list:
    """Recent confirmed txs for one address; net amount from this address's
    perspective (received outputs minus spent inputs)."""
    rows = []
    for tx in http_json(f"https://mempool.space/api/address/{addr}/txs")[:limit]:
        received = sum(o["value"] for o in tx["vout"]
                       if o.get("scriptpubkey_address") == addr)
        spent = sum(i["prevout"]["value"] for i in tx["vin"]
                    if (i.get("prevout") or {}).get("scriptpubkey_address") == addr)
        when = (tx.get("status") or {}).get("block_time")
        day = (datetime.fromtimestamp(when, tz=timezone.utc).strftime("%Y-%m-%d")
               if when else "pending")
        rows.append((day, "BTC", (received - spent) / 1e8, tx["txid"]))
    return rows


def eth_history(addr: str, limit: int) -> list:
    """Recent native-ETH txs via Blockscout's keyless public API
    (JSON-RPC nodes can't list transactions by address; an indexer can)."""
    resp = http_json("https://eth.blockscout.com/api/v2/addresses/"
                     f"{addr}/transactions")
    rows = []
    for tx in resp.get("items", [])[:limit]:
        value = int(tx.get("value") or 0) / 1e18
        if tx["from"]["hash"].lower() == addr.lower():
            value = -value
        rows.append((tx["timestamp"][:10], "ETH", value, tx["hash"]))
    return rows


def cmd_history(args) -> None:
    if not CRYPTO_FILE.exists():
        sys.exit(f"No {CRYPTO_FILE} yet -- run `plaidexpenses.py crypto`"
                 " first to create it.")
    cfg = load_json(CRYPTO_FILE, {})
    wallets = [w for w in cfg.get("wallets", []) if "..." not in w["address"]]
    if not wallets:
        sys.exit("No wallet addresses configured in crypto.json.")
    print(f"{'date':<12} {'wallet':<20} {'asset':<6} {'net amount':>18}  txid")
    print("-" * 88)
    for wallet in wallets:
        chain = wallet["chain"].lower()
        label = wallet.get("label", wallet["address"][:12])
        try:
            fetch = {"btc": btc_history, "eth": eth_history}[chain]
        except KeyError:
            print(f"warning: {label}: history not supported for chain"
                  f" {chain!r}", file=sys.stderr)
            continue
        try:
            for day, asset, net, txid in fetch(wallet["address"], args.limit):
                print(f"{day:<12} {label:<20} {asset:<6} {net:>18,.8f} "
                      f" {txid[:16]}…")
        except Exception as e:
            print(f"warning: {label} history fetch failed: {e}",
                  file=sys.stderr)


def spot_usd(asset: str, day: str = None) -> float:
    """Spot USD price via Coinbase's public (unauthenticated) price API;
    pass day=YYYY-MM-DD for the historical price on that date."""
    if asset in ("USD", "USDC"):
        return 1.0
    url = f"https://api.coinbase.com/v2/prices/{asset}-USD/spot"
    if day:
        url += f"?date={day}"
    return float(http_json(url)["data"]["amount"])


def sync_crypto(store: dict) -> None:
    """Pull crypto transactions into the shared expense store, valued in USD.
    Store convention follows Plaid: positive amount = money out. Keys are
    prefixed and deterministic, so re-syncing overwrites rather than dupes."""
    cfg = load_json(CRYPTO_FILE, {})
    key = None
    try:
        key = coinbase_key(cfg)
    except Exception as e:
        print(f"warning: coinbase key unavailable: {e}", file=sys.stderr)
    if key:
        count = 0
        try:
            for acct in coinbase_paged(key, "/v2/accounts?limit=100"):
                for tx in coinbase_paged(
                        key, f"/v2/accounts/{acct['id']}/transactions?limit=100"):
                    usd = float((tx.get("native_amount") or {}).get("amount")
                                or 0)
                    details = tx.get("details") or {}
                    name = details.get("title") or f"coinbase {tx['type']}"
                    if details.get("subtitle"):
                        name += f" ({details['subtitle']})"
                    store["cb-" + tx["id"]] = {
                        "date": tx["created_at"][:10],
                        "name": name,
                        "amount": -usd,  # Coinbase: positive = credit to acct
                        "currency": "USD",
                        "category": "CRYPTO_EXCHANGE",
                        "account_id": acct["id"],
                        "pending": tx.get("status") != "completed",
                    }
                    count += 1
            print(f"coinbase: {count} transactions")
        except Exception as e:
            print(f"warning: coinbase transaction sync failed: {e}",
                  file=sys.stderr)

    prices: dict[tuple, float] = {}
    for wallet in cfg.get("wallets", []):
        if "..." in wallet["address"]:
            continue
        chain = wallet["chain"].lower()
        label = wallet.get("label", wallet["address"][:12])
        fetch = {"btc": btc_history, "eth": eth_history}.get(chain)
        if not fetch:
            print(f"warning: {label}: tx sync unsupported for {chain!r}",
                  file=sys.stderr)
            continue
        try:
            count = 0
            for day, asset, net, txid in fetch(wallet["address"], 100):
                pending = day == "pending"
                day = date.today().isoformat() if pending else day
                if (asset, day) not in prices:
                    prices[(asset, day)] = spot_usd(asset, day)
                store[f"{chain}-{txid}-{wallet['address'][-8:]}"] = {
                    "date": day,
                    "name": f"{label}: on-chain {asset} transfer",
                    "amount": -net * prices[(asset, day)],
                    "currency": "USD",
                    "category": "CRYPTO_ONCHAIN",
                    "account_id": wallet["address"],
                    "pending": pending,
                }
                count += 1
            print(f"{label}: {count} on-chain transactions")
        except Exception as e:
            print(f"warning: {label} tx sync failed: {e}", file=sys.stderr)


def cmd_crypto(_args) -> None:
    if not CRYPTO_FILE.exists():
        save_json(CRYPTO_FILE, CRYPTO_TEMPLATE, private=True)
        sys.exit(f"Created config template at {CRYPTO_FILE} -- add your"
                 " Coinbase CDP key path and/or watch-only wallet addresses,"
                 " then rerun.")
    cfg = load_json(CRYPTO_FILE, {})
    rows = []
    try:
        rows += coinbase_holdings(cfg)
    except Exception as e:  # one failing source shouldn't hide the others
        print(f"warning: coinbase fetch failed: {e}", file=sys.stderr)
    for wallet in cfg.get("wallets", []):
        if "..." in wallet["address"]:
            continue  # template placeholder
        try:
            rows.append(wallet_holding(wallet, cfg))
        except Exception as e:
            print(f"warning: {wallet.get('label', wallet['address'])}"
                  f" fetch failed: {e}", file=sys.stderr)
    if not rows:
        sys.exit("No holdings found -- check crypto.json configuration.")

    prices = {}
    for _, asset, _ in rows:
        if asset not in prices:
            try:
                prices[asset] = spot_usd(asset)
            except Exception:
                prices[asset] = None

    print(f"{'source':<20} {'asset':<8} {'quantity':>16} {'price':>12}"
          f" {'value USD':>14}")
    print("-" * 74)
    total = 0.0
    for source, asset, qty in sorted(
            rows, key=lambda r: (prices.get(r[1]) or 0) * r[2], reverse=True):
        price = prices.get(asset)
        if price is None:
            print(f"{source:<20} {asset:<8} {qty:>16,.8f} {'?':>12} {'?':>14}")
            continue
        value = qty * price
        total += value
        print(f"{source:<20} {asset:<8} {qty:>16,.8f} {price:>12,.2f}"
              f" {value:>14,.2f}")
    print("-" * 74)
    print(f"{'total':<20} {'':<8} {'':>16} {'':>12} {total:>14,.2f}")


# ---------------------------------------------------------------- csv

def cmd_csv(_args) -> None:
    store = load_json(TX_FILE, {})
    if not store:
        sys.exit("No transactions stored. Run `plaidexpenses.py sync` first.")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["date", "name", "amount", "currency", "category",
                     "pending"])
    for tx in sorted(store.values(), key=lambda t: t["date"]):
        writer.writerow([tx["date"], tx["name"], tx["amount"], tx["currency"],
                         tx["category"], tx["pending"]])
    sys.stdout.write(out.getvalue())


# ---------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="look up Plaid coverage for banks")
    check.add_argument("banks", nargs="+", metavar="BANK",
                       help="institution name(s) to search for")
    sub.add_parser("link", help="connect an account via Plaid Hosted Link")
    sub.add_parser("accounts", help="list accounts and balances")
    sub.add_parser("sync", help="pull new transactions")
    report = sub.add_parser("report", help="monthly spend vs. budgets")
    report.add_argument("--month", metavar="YYYY-MM",
                        help="month to report on (default: current)")
    sub.add_parser("csv", help="dump transactions as CSV to stdout")
    sub.add_parser("crypto", help="Coinbase + watch-only wallet snapshot")
    history = sub.add_parser("history",
                             help="on-chain transaction history per wallet")
    history.add_argument("--limit", type=int, default=25, metavar="N",
                         help="max transactions per wallet (default 25)")
    args = parser.parse_args()
    {"check": cmd_check, "link": cmd_link, "accounts": cmd_accounts,
     "sync": cmd_sync, "report": cmd_report, "csv": cmd_csv,
     "crypto": cmd_crypto, "history": cmd_history}[args.command](args)


if __name__ == "__main__":
    main()
