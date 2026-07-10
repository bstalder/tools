# tools

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
