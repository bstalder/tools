# tools

## plaidexpenses.py

A Mint-style personal expense tracker built on the [Plaid API](https://plaid.com/docs/).
Link bank/card accounts once via [Plaid Hosted Link](https://plaid.com/docs/link/hosted-link/)
in the browser, then sync transactions incrementally and get monthly
spending-vs-budget reports in the terminal. Python 3.9+ standard library
only — no pip installs. Hosted Link means OAuth institutions (Chase,
Schwab, Fidelity, Capital One, BofA, Wells Fargo, Citi, …) work without
registering redirect URIs or running any web server.

### Setup

1. Create a (free) account at [dashboard.plaid.com](https://dashboard.plaid.com)
   and grab your client id and sandbox secret from **Developers → Keys**.
   Sandbox uses fake institutions (user `user_good`, password `pass_good`)
   so you can try the whole flow immediately. To connect real accounts,
   apply for production access from the same dashboard (free tier available
   for personal use; approval usually takes a few days).
2. Export credentials — the secret can be a 1Password secret reference,
   which is resolved via `op read` at runtime so it never sits in your
   shell profile in plaintext:

   ```sh
   export PLAID_CLIENT_ID=xxxx
   export PLAID_SECRET="op://Private/Plaid/sandbox-secret"
   export PLAID_ENV=sandbox   # or production
   ```

### Usage

```
./plaidexpenses.py check BANK...     # Plaid coverage / OAuth status lookup
./plaidexpenses.py link              # connect an account (opens browser)
./plaidexpenses.py accounts          # balances per linked account
./plaidexpenses.py sync              # pull new/changed transactions
./plaidexpenses.py report            # this month's spend vs. budgets
./plaidexpenses.py report --month 2026-06
./plaidexpenses.py csv > all.csv     # export the full transaction store
```

Coverage check for the target institution list:

```sh
./plaidexpenses.py check schwab fidelity chase "wells fargo" citibank \
  "oneaz" "hsa bank" "caltech employees" "capital one" comenity \
  "bank of america"
```

Run `link` once per institution; each linked item syncs independently.

### Crypto

Plaid doesn't cover exchanges or wallets, so `crypto` handles those
directly:

```
./plaidexpenses.py crypto            # portfolio snapshot with USD values
./plaidexpenses.py history           # on-chain tx history per wallet
./plaidexpenses.py history --limit 50
```

The first run writes `~/.local/share/plaid-expenses/crypto.json`. Two
kinds of sources go in it:

- **Coinbase** — create a **read-only** ES256 API key at
  [portal.cdp.coinbase.com](https://portal.cdp.coinbase.com) (View
  permission only; never grant Trade/Transfer for this). Set
  `coinbase_key` to the downloaded key-JSON's path, or to an `op://`
  1Password reference holding its contents. Requests are signed with
  ES256 JWTs via `openssl` — no pip packages needed.
- **Self-custody wallets** — watch-only: list public addresses with
  `"chain": "btc"` or `"chain": "eth"`. Balances come from public data
  (mempool.space / an Ethereum RPC), transaction history from
  mempool.space and Blockscout, spot prices from Coinbase's public price
  API. Only native BTC/ETH activity is tracked, not ERC-20 tokens.

Watch-only caveats: everything at an address is public (that's what makes
this work keylessly), but modern BTC wallets rotate addresses — a single
address shows only part of a wallet's activity unless you reuse
addresses; full-wallet tracking needs xpub support (not implemented).
Querying these public APIs also tells those services which addresses your
IP is interested in — run your own node/indexer if that association
matters to you.

`sync` also ingests crypto activity into the shared transaction store, so
`report` and `csv` include it: Coinbase transactions arrive as
`CRYPTO_EXCHANGE` (USD value at execution time, straight from the API)
and on-chain transfers as `CRYPTO_ONCHAIN` (valued at that date's spot
price via Coinbase's historical price API). Re-syncing is idempotent —
deterministic keys mean updates overwrite rather than duplicate.

Interpretation caveats for the report:

- **Self-transfers appear on both sides.** A Coinbase→Ledger withdrawal
  is a `CRYPTO_EXCHANGE` debit *and* a `CRYPTO_ONCHAIN` credit; they
  roughly net out but inflate both gross totals.
- **A Coinbase buy funded from a bank account also shows up twice** —
  once from Plaid (the bank debit) and once as `CRYPTO_EXCHANGE`. Watch
  for that when reading `net`.
- On-chain sync covers the most recent ~50 transactions per address per
  run; sync regularly to keep continuous history.

**Never put private keys or seed phrases in any config for any tool.**
Watch-only addresses and a View-scoped API key are all this needs, and
all it should ever have.

The first `report` run writes a budget template to
`~/.local/share/plaid-expenses/budgets.json`; edit it with monthly limits
keyed by Plaid category name (categories appearing in your data are shown
in the report, so run it once to see what to budget).

### Security notes

- **Your bank credentials never touch this script.** Linking happens
  entirely on Plaid's hosted page (OAuth banks redirect to the bank's own
  login); the script only ever receives an access token, stored
  `chmod 600` in `~/.local/share/plaid-expenses/items.json`.
- That token grants read access to your transactions — treat the data
  directory like a password. It lives outside the repo by design.

## startopenvpn.sh

Connect to an OpenVPN server that requires username/password plus a TOTP
2FA code (delivered via OpenVPN's `static-challenge` mechanism), fetching
the TOTP automatically from 1Password so you never have to type a code.

### How it works

When a VPN profile contains a `static-challenge` directive, the OpenVPN
client normally stops and prompts you for the authenticator code, then
combines it with your password into a single string:

```
SCRV1:base64(password):base64(otp)
```

and sends that as the password. This script builds that string itself —
password from a local credentials file, OTP from the 1Password CLI — and
runs openvpn against a temporary copy of the profile with the
`static-challenge` line removed, so the whole connection is prompt-free
(aside from 1Password's unlock/biometric approval).

Temporary files holding credentials are created with owner-only
permissions and deleted when openvpn exits.

### Prerequisites

- OpenVPN 2.x client (`openvpn` or MacPorts' `openvpn2`)
- [1Password CLI](https://developer.1password.com/docs/cli/) (`op`),
  signed in / integrated with the desktop app
- A 1Password item containing the TOTP secret for your VPN account
- Your `.ovpn` profile, with `static-challenge` in the default `scrv1`
  format (if your server uses `concat` format, adapt the `printf` in
  `connect()` to `password+otp` concatenation instead)

### Setup

1. Create a credentials file (e.g. `~/.openvpnauth.txt`) with your VPN
   username on line 1 and password on line 2, and lock it down:

   ```
   chmod 600 ~/.openvpnauth.txt
   ```

2. Edit the configuration block at the top of the script, or override via
   environment variables:

   | Variable       | Meaning                                    |
   |----------------|--------------------------------------------|
   | `OP_BIN`       | path to the 1Password CLI                  |
   | `OPENVPN_BIN`  | path to the openvpn binary                 |
   | `VPN_PROFILE`  | path to your `.ovpn` profile               |
   | `VPN_AUTHFILE` | path to the username/password file         |
   | `OP_ITEM`      | 1Password item name holding the TOTP       |

3. (Optional, needed for `--loop`) Let openvpn start without a sudo
   password prompt by adding a narrowly-scoped sudoers rule via
   `sudo visudo`:

   ```
   yourusername ALL=(root) NOPASSWD: /opt/local/sbin/openvpn2
   ```

### Usage

```
./startopenvpn.sh          # connect once; exits when the tunnel closes
./startopenvpn.sh --loop   # reconnect automatically whenever it closes
```

Loop mode is for servers that force a disconnect every 24 hours: when the
tunnel drops, the script fetches a fresh OTP and reconnects (you'll get a
1Password approval prompt once per cycle). Three rapid failures in a row
abort the loop rather than hammering the server. Stop it with Ctrl-C.

### Security notes

- **Never commit your `.ovpn` profile** — user-locked profiles contain
  your private key inline. The `.gitignore` here blocks `*.ovpn` as a
  guardrail.
- **Never commit the credentials file.** Keep it `chmod 600`, outside
  the repo.
- Fully unattended operation (e.g. a 1Password service-account token on
  disk) would let anyone who compromises the machine obtain both auth
  factors — the once-per-day biometric approval is the deliberate
  trade-off.
