#!/bin/sh
# Connect to a pinglamp with plain nc, in raw mode so single keys register.
# (telnet does this for you via option negotiation; nc needs a nudge.)
#
#   ./connect.sh                 -> 127.0.0.1 2323
#   ./connect.sh lamp.host 2323

HOST="${1:-127.0.0.1}"
PORT="${2:-2323}"

if ! command -v nc >/dev/null 2>&1; then
    echo "nc not found - try:  telnet $HOST $PORT" >&2
    exit 1
fi

old_stty=$(stty -g)
restore() {
    stty "$old_stty" 2>/dev/null
    printf '\033[?25h\033[0m\n'
}
trap restore EXIT INT TERM

stty raw -echo
nc "$HOST" "$PORT"
