# pinglamp

just a lamp, turn it on, turn it off.

a shared virtual lamp. Anyone who connects can flip it ON or OFF. Everyone
currently connected sees it happen live, the instant anyone else flips it.

No accounts. No auth. No hardware. One boolean in server memory, broadcast
over a raw TCP socket, dressed like a 1993 BBS.

```
╔════════════════════════════════════════════════════════════════════════════╗
║  P I N G L A M P  ▓▒░ a shared bulb on a socket ░▒▓                 :2323  ║
╠════════════════════════════════════════════════════════════════════════════╣
║                           \    \    |    /    /                            ║
║                            '.   \   |   /   .'                             ║
║                                .-'''''''-.                                 ║
║                              .'▒▒▒▒▒▒▒▒▒▒▒'.                               ║
║                             /▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒\                              ║
║                            |▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒|                             ║
║                             \▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒/                              ║
║                              '.▒▒▒▒▒▒▒▒▒▒▒.'                               ║
║                                '.═══════.'                                 ║
║                                 |███████|                                  ║
║                         >>>   THE LAMP IS ON   <<<                         ║
╠════════════════════════════════════════════════════════════════════════════╣
║ last flip brass_candle just now             flips 1            up 00:00:03 ║
║ souls 2 » brass_candle*, midnight_relay                                    ║
╠════════════════════════════════════════════════════════════════════════════╣
║ [09:19:38] · brass_candle joined                                           ║
║ [09:19:39] · midnight_relay joined                                         ║
║ [09:19:40] ▲ brass_candle turned the lamp ON                               ║
╠════════════════════════════════════════════════════════════════════════════╣
║ [SPACE] flip   [N] new handle   [H] help   [Q] quit   you are brass_candle ║
╚════════════════════════════════════════════════════════════════════════════╝
```

## Run it

```sh
python3 pinglamp.py                 # listens on 0.0.0.0:2323
```

Stdlib only. Python 3.9+. No install, no dependencies, no build step.

## Connect

```sh
telnet localhost 2323               # best experience
./connect.sh localhost 2323         # if you'd rather use nc
```

Open two terminals and connect twice. Hit SPACE in one and watch the other.

| key | does |
| --- | --- |
| `SPACE` / `ENTER` | flip the lamp |
| `L` | the log — every flip, when, and by whom |
| `N` | roll a new handle |
| `H` | help page (any key returns) |
| `Q` / `Ctrl-C` | hang up |

## The log

Live presence only answers "is my friend flipping it *right now*". `L` answers
"did anything happen while I was asleep":

```
╔════════════════════════════════════════════════════════════════════════════╗
║  P I N G L A M P  · the log ·                  last 24h 13 flips, 2 souls  ║
╠════════════════════════════════════════════════════════════════════════════╣
║  when             who                 what            ago                  ║
║  Thu 08:39:22  ▲  brass_candle        turned it ON    1h ago               ║
║  Thu 01:39:01  ▲  brass_candle        turned it ON    7h ago               ║
║  Thu 00:51:16  ▼  amber_wren          turned it off   8h ago               ║
║  Wed 13:39:13  ▲  amber_wren          turned it ON    19h ago              ║
╠════════════════════════════════════════════════════════════════════════════╣
║  24h ago ···▄▂··········█▂·····▅▂ now                 one cell = one hour  ║
╚════════════════════════════════════════════════════════════════════════════╝
```

Every flip is appended to `lamp.log` as one JSON object per line, so it's
`tail -f`-able and greppable outside the BBS:

```sh
tail -f lamp.log
{"t": 1755074362.114, "who": "amber_wren", "on": true}
```

The server keeps the last 500 flips in memory for rendering and reads the
tail of the file at startup, so history survives a restart. `--log ''` to
keep no history at all.

## How it works

The whole thing is one boolean and a list of writers.

- **`asyncio.start_server`** — a plain TCP listener. Nothing above it: no
  HTTP, no WebSocket, no framing protocol. The wire carries ANSI escape
  codes and that is all.
- **Telnet character mode** — on connect the server sends
  `IAC WILL ECHO`, `IAC WILL SGA`, `IAC DO SGA`, which talks a real telnet
  client out of line-buffering. That's what makes a bare SPACE arrive
  immediately instead of waiting for Enter. Incoming `IAC` sequences are
  parsed out of the keystroke stream by a small state machine
  (`Client.decode`) that survives being split across packets.
- **`TCP_NODELAY`** — set on every socket, so a flip leaves the server the
  moment it's decided rather than waiting on Nagle's algorithm.
- **Push, not poll** — a flip mutates the boolean and immediately writes a
  fresh 24-row frame to every connected writer. Clients never ask for
  anything. A 1 Hz ticker redraws too, only so the clock and the
  "4s ago" stay honest for idle watchers.
- **Frames, not scrollback** — each render is `ESC[H` (cursor home) plus 24
  rows each ending in `ESC[K` (clear to end of line). Nothing scrolls; the
  screen is repainted in place. Every row is padded to exactly 78 visible
  columns by an ANSI-aware `pad()`, so colour codes never throw off the
  alignment.

Handles are dice, not identity: an adjective and a noun, rolled at connect,
unique among whoever is currently on. Nothing about you is stored.

The lamp's state, its flip count, and who touched it last are saved to
`lamp.state` so it survives a restart. Pass `--state ''` if you'd rather it
forget.

```sh
python3 pinglamp.py --host 127.0.0.1 --port 2323 --max-clients 64
```

## Test

```sh
python3 test_smoke.py
```

Spawns a server on a scratch port, connects two real sockets, and asserts
the thing that actually matters: a flip sent by one socket shows up on the
other without it asking. Also covers telnet byte parsing, CRLF counting as
one flip rather than two, the help screen, departures, and state surviving
a restart.

## Letting a friend in

Running the server does **not** put it on the internet. `--host 0.0.0.0`
means "every interface on this machine", which is not the same as "reachable
from outside". Three cases:

- **Same LAN** — works right now. They connect to your `192.168.x.x` address.
- **Router port-forward** — classic, but unavailable behind carrier NAT or a
  full-tunnel VPN, where the address the world sees is a shared exit rather
  than a port you can forward.
- **A tunnel** — the practical answer, below.

### Quick setup

Tailscale puts both machines on a WireGuard mesh talking over `100.x`
addresses. Nothing is exposed publicly, the traffic is encrypted, and NAT
stops mattering.

**Steps 1–6 happen once, ever. Steps 7–8 are every time.**

*You:*

1. Install it (Tailscale is in the Debian/Kali repos):
   ```sh
   sudo apt install tailscale
   sudo systemctl enable --now tailscaled
   ```
2. Log in — prints a link, open it, claim the machine:
   ```sh
   sudo tailscale up
   ```
3. Note your address:
   ```sh
   tailscale ip -4          # 100.x.y.z
   ```
4. Share this one machine: admin console → **Machines** → your machine →
   **⋯** → **Share…**. Send your friend the link it gives you.

*Your friend:*

5. Installs Tailscale ([tailscale.com/download](https://tailscale.com/download))
   and runs `tailscale up`, logging in with any Google/GitHub/Microsoft
   account — nothing to create.
6. Opens your share link and accepts. Your machine now appears in their
   `tailscale status`.

*Then, every time:*

7. You start the lamp:
   ```sh
   python3 pinglamp.py --host "$(tailscale ip -4)"
   ```
8. They connect:
   ```sh
   telnet 100.x.y.z 2323                          # from `tailscale ip -4`
   telnet your-machine.your-tailnet.ts.net 2323   # if MagicDNS is on
   ```

**No login in step 7 or 8, ever.** Tailscale authenticates the *device*, not
the session, so it survives reboots and restarting the lamp re-prompts
nothing. The lamp itself never asks for anything at all — it's telnet, and it
has no accounts by design.

### The fine print

- **Share, don't invite.** Step 4 shares one device. Inviting your friend into
  your tailnet would hand them every machine on it. Sharing works on the free
  plan, and shared machines are quarantined by default — their node can reach
  the lamp, the lamp can't reach back into their network.
- **`--host "$(tailscale ip -4)"`** binds the lamp to the tailnet interface
  only, so the LAN and the public internet see nothing.
- **Node keys expire after 180 days** and the device must re-authenticate.
  For an always-on lamp host, turn that off: admin console → **Machines** →
  your machine → **⋯** → **Disable key expiry**.
- **They need a telnet client**: built in on Linux, `brew install telnet` or
  `./connect.sh` on macOS, PuTTY in **Telnet** mode (not SSH) on Windows.
- **Already on a VPN?** Tailscale normally coexists — it only claims
  `100.64.0.0/10` — but if your client captures every route, check
  `tailscale ping <friend>` before blaming the lamp.
- **If they won't install anything**, a public TCP tunnel (`ngrok tcp 2323`
  and friends) needs nothing but telnet on their end. The trade is real: that
  address is on the public internet in plaintext, and anyone who learns it
  can flip your lamp.

### If you'd rather it stayed up 24/7

Tailscale only reaches machines that are awake. If you want your friend to be
able to flip it at 3am and have you read it over breakfast, run the server on
a small always-on box (a VPS, a Pi) and have *both* of you connect to it as
clients. The lamp is the neutral thing in the middle; that's what the log is
for.

## Ideas

- `--lamp <name>` for multiple lamps, one per port or one per keyword
- an HTTP endpoint returning `{"on": true}` so other things can read the lamp
- a desktop notification when the lamp changes while you're not looking
- a physical bulb on a relay, subscribed to the same socket
