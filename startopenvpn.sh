#!/bin/bash
#
# startopenvpn.sh — connect to an OpenVPN server that uses static-challenge
# 2FA, fetching the TOTP code from 1Password automatically.
#
# The server's profile uses `static-challenge` (scrv1 format): the OpenVPN
# client normally prompts for the OTP and sends
#   SCRV1:base64(password):base64(otp)
# as the password. This script builds that string itself so no interactive
# prompt is needed.
#
# Usage:
#   startopenvpn.sh          connect once, exit when the tunnel closes
#   startopenvpn.sh --loop   reconnect automatically each time the tunnel
#                            closes (e.g. a server-enforced 24h cycle)
#
set -euo pipefail

# ---- Configuration — edit the defaults or override via environment ----
OP_BIN="${OP_BIN:-$(command -v op || echo /opt/local/bin/op)}"
OPENVPN_BIN="${OPENVPN_BIN:-$(command -v openvpn2 || command -v openvpn || echo /opt/local/sbin/openvpn2)}"
VPN_PROFILE="${VPN_PROFILE:-$HOME/sonomainstallfiles/profile-userlocked.ovpn}"
VPN_AUTHFILE="${VPN_AUTHFILE:-$HOME/.openvpnauth.txt}"   # line 1: username, line 2: password
OP_ITEM="${OP_ITEM:-lsstovpn}"                           # 1Password item holding the TOTP
# -----------------------------------------------------------------------

LOOP=0
case "${1:-}" in
    --loop) LOOP=1 ;;
    "") ;;
    *) echo "usage: $(basename "$0") [--loop]" >&2; exit 2 ;;
esac

for f in "$OP_BIN" "$OPENVPN_BIN"; do
    [[ -x "$f" ]] || { echo "error: required binary not found: $f" >&2; exit 1; }
done
[[ -r "$VPN_PROFILE" ]]  || { echo "error: VPN profile not found: $VPN_PROFILE" >&2; exit 1; }
[[ -r "$VPN_AUTHFILE" ]] || { echo "error: auth file not found: $VPN_AUTHFILE" >&2; exit 1; }

umask 077
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
trap 'exit 130' INT TERM

# Profile copy without the static-challenge directive, so openvpn doesn't
# stop to prompt for the OTP.
grep -v '^static-challenge' "$VPN_PROFILE" > "$WORKDIR/profile.ovpn"

connect() {
    local otp username password
    otp="$("$OP_BIN" item get --otp "$OP_ITEM")"
    username="$(sed -n 1p "$VPN_AUTHFILE")"
    password="$(sed -n 2p "$VPN_AUTHFILE")"

    printf '%s\nSCRV1:%s:%s\n' \
        "$username" \
        "$(printf %s "$password" | base64)" \
        "$(printf %s "$otp" | base64)" > "$WORKDIR/auth"

    sudo "$OPENVPN_BIN" --client --config "$WORKDIR/profile.ovpn" \
        --auth-user-pass "$WORKDIR/auth"
}

if (( ! LOOP )); then
    connect
    exit
fi

# Loop mode: reconnect whenever the tunnel closes. An exit within 60s counts
# as a failure (bad auth, unreachable server); three in a row aborts rather
# than hammering the server.
failures=0
while true; do
    started=$(date +%s)
    connect || true
    elapsed=$(( $(date +%s) - started ))
    if (( elapsed < 60 )); then
        failures=$(( failures + 1 ))
        if (( failures >= 3 )); then
            echo "error: 3 consecutive rapid exits — check credentials/server, giving up" >&2
            exit 1
        fi
        echo "tunnel closed after ${elapsed}s (failure $failures/3), retrying in 10s..." >&2
        sleep 10
    else
        failures=0
        echo "tunnel closed after $(( elapsed / 3600 ))h$(( (elapsed % 3600) / 60 ))m, reconnecting..." >&2
    fi
done
