#!/usr/bin/env python3
"""
pinglamp - one lamp, many hands.

A single boolean living in server memory, hung on a raw TCP socket and
dressed up like a 1993 BBS. Anyone who connects can flip it. Everyone
who is connected watches it flip, the instant it happens.

    python3 pinglamp.py            # listen on 0.0.0.0:2323
    telnet localhost 2323          # become a soul

No accounts. No auth. No database worth the name. Just the lamp.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import random
import re
import signal
import socket
import time

# --------------------------------------------------------------------------
# telnet
#
# We speak just enough of RFC 854 to talk a real telnet client out of
# line-buffering, so a single tap of SPACE reaches us without an Enter.
# --------------------------------------------------------------------------

IAC = 255   # interpret as command
SE = 240    # end subnegotiation
SB = 250    # begin subnegotiation
WILL, WONT, DO, DONT = 251, 252, 253, 254
OPT_ECHO = 1
OPT_SGA = 3  # suppress go-ahead

# "I will echo, I will suppress go-ahead, and so should you" - the classic
# incantation that puts a telnet client into character-at-a-time mode.
NEGOTIATE = bytes([
    IAC, WILL, OPT_ECHO,
    IAC, WILL, OPT_SGA,
    IAC, DO, OPT_SGA,
])

# --------------------------------------------------------------------------
# screen
# --------------------------------------------------------------------------

WIDTH = 78
INNER = WIDTH - 2
ROWS = 24

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
REV = "\x1b[7m"

GREY = "\x1b[90m"
RED = "\x1b[91m"
GREEN = "\x1b[92m"
YELLOW = "\x1b[93m"
BLUE = "\x1b[94m"
MAGENTA = "\x1b[95m"
CYAN = "\x1b[96m"
WHITE = "\x1b[97m"
DYELLOW = "\x1b[33m"

FRAME = CYAN

CLEAR = "\x1b[2J\x1b[H"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Ten rows tall, both states, so the frame never moves when it flips.
# Rows 0-1 are the light rays, rows 2-7 the glass, rows 8-9 the fitting.
ART_ON = [
    r"   \    \    |    /    /   ",
    r"    '.   \   |   /   .'    ",
    r"        .-'''''''-.        ",
    r"      .'▒▒▒▒▒▒▒▒▒▒▒'.      ",
    r"     /▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒\     ",
    r"    |▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒|    ",
    r"     \▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒/     ",
    r"      '.▒▒▒▒▒▒▒▒▒▒▒.'      ",
    r"        '.═══════.'        ",
    r"         |███████|         ",
]

ART_OFF = [
    r"                           ",
    r"                           ",
    r"        .-'''''''-.        ",
    r"      .'           '.      ",
    r"     /               \     ",
    r"    |        .        |    ",
    r"     \      ' '      /     ",
    r"      '.           .'      ",
    r"        '.-------.'        ",
    r"         |███████|         ",
]

ADJECTIVES = [
    "oxide", "vagrant", "dim", "amber", "hollow", "static", "quiet", "brass",
    "loose", "midnight", "paper", "salt", "velvet", "rusted", "spare", "pale",
    "cobalt", "tin", "wandering", "half", "soft", "glass", "north", "damp",
]
NOUNS = [
    "hare", "moth", "pixel", "kettle", "signal", "ferret", "lantern", "crow",
    "socket", "otter", "beacon", "wren", "filament", "badger", "relay", "owl",
    "ember", "heron", "switch", "vole", "candle", "magpie", "fuse", "stoat",
]

# --------------------------------------------------------------------------
# text helpers (ANSI-aware, because every row must be exactly INNER wide)
# --------------------------------------------------------------------------


def _tokens(s: str):
    i = 0
    for m in ANSI_RE.finditer(s):
        for ch in s[i:m.start()]:
            yield True, ch
        yield False, m.group()
        i = m.end()
    for ch in s[i:]:
        yield True, ch


def vlen(s: str) -> int:
    return sum(1 for visible, _ in _tokens(s) if visible)


def trunc(s: str, n: int) -> str:
    out, used = [], 0
    for visible, tok in _tokens(s):
        if not visible:
            out.append(tok)
        elif used < n:
            out.append(tok)
            used += 1
    return "".join(out)


def pad(s: str, n: int = INNER) -> str:
    have = vlen(s)
    if have > n:
        return trunc(s, n) + RESET
    return s + " " * (n - have)


def centre(s: str, n: int = INNER) -> str:
    have = vlen(s)
    if have >= n:
        return pad(s, n)
    left = (n - have) // 2
    return " " * left + s + " " * (n - have - left)


def block(lines: list[str], n: int = INNER) -> list[str]:
    """Centre a multi-line block as one unit, so it doesn't shear."""
    widest = max(vlen(l) for l in lines)
    left = " " * max(0, (n - widest) // 2)
    return [pad(left + l, n) for l in lines]


def ago(seconds: float) -> str:
    s = int(seconds)
    if s < 1:
        return "just now"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------
# a connected soul
# --------------------------------------------------------------------------


class Client:
    __slots__ = ("writer", "handle", "mode", "last_flip", "_ts", "joined")

    def __init__(self, writer: asyncio.StreamWriter, handle: str):
        self.writer = writer
        self.handle = handle
        self.mode = "lamp"       # or "help"
        self.last_flip = 0.0
        self.joined = time.time()
        self._ts = 0             # telnet parser state

    def decode(self, data: bytes) -> bytes:
        """Strip telnet IAC sequences, return the keystrokes underneath."""
        out = bytearray()
        for b in data:
            if self._ts == 0:
                if b == IAC:
                    self._ts = 1
                else:
                    out.append(b)
            elif self._ts == 1:              # after IAC
                if b == IAC:
                    out.append(IAC)          # escaped 0xFF
                    self._ts = 0
                elif b in (WILL, WONT, DO, DONT):
                    self._ts = 2             # option byte follows
                elif b == SB:
                    self._ts = 3
                else:
                    self._ts = 0
            elif self._ts == 2:              # swallow option byte
                self._ts = 0
            elif self._ts == 3:              # inside subnegotiation
                if b == IAC:
                    self._ts = 4
            elif self._ts == 4:
                self._ts = 3 if b != SE else 0
        return bytes(out)

    def send(self, text: str) -> None:
        w = self.writer
        if w.is_closing():
            return
        tr = w.transport
        # Don't grow a frame backlog for a client that stopped reading.
        if tr is not None and tr.get_write_buffer_size() > 256 * 1024:
            return
        try:
            w.write(text.encode("utf-8", "replace"))
        except (ConnectionError, RuntimeError):
            pass


# --------------------------------------------------------------------------
# the lamp
# --------------------------------------------------------------------------


class PingLamp:
    FLIP_COOLDOWN = 0.12  # per-soul, so a held key doesn't strobe the room

    def __init__(self, host: str, port: int, statefile: str | None,
                 logfile: str | None, maxclients: int):
        self.host = host
        self.port = port
        self.statefile = statefile
        self.logfile = logfile
        self.maxclients = maxclients

        self.on = False
        self.flips = 0
        self.last_flipper: str | None = None
        self.last_flip_at: float | None = None
        self.started = time.time()

        self.clients: list[Client] = []
        self.feed: collections.deque[tuple[float, str, str]] = collections.deque(maxlen=3)
        # Durable record lives in the file; this is the view we render from.
        self.history: collections.deque[tuple[float, str, bool]] = collections.deque(maxlen=500)
        self._load()
        self._load_log()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.statefile or not os.path.exists(self.statefile):
            return
        try:
            with open(self.statefile) as f:
                data = json.load(f)
            self.on = bool(data.get("on", False))
            self.flips = int(data.get("flips", 0))
            self.last_flipper = data.get("last_flipper")
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        if not self.statefile:
            return
        try:
            tmp = self.statefile + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"on": self.on, "flips": self.flips,
                           "last_flipper": self.last_flipper}, f)
            os.replace(tmp, self.statefile)
        except OSError:
            pass

    def _load_log(self) -> None:
        """Read the tail of the flip log, so history survives a restart."""
        if not self.logfile or not os.path.exists(self.logfile):
            return
        try:
            with open(self.logfile, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                f.seek(max(0, size - 128 * 1024))
                chunk = f.read()
            lines = chunk.decode("utf-8", "replace").splitlines()
            if size > 128 * 1024 and lines:
                lines = lines[1:]              # drop the half-line we sliced into
            for line in lines:
                try:
                    e = json.loads(line)
                    self.history.append((float(e["t"]), str(e["who"]), bool(e["on"])))
                except (ValueError, KeyError, TypeError):
                    continue
        except OSError:
            pass

    def _append_log(self, ts: float, who: str, on: bool) -> None:
        if not self.logfile:
            return
        try:
            with open(self.logfile, "a") as f:
                f.write(json.dumps({"t": round(ts, 3), "who": who, "on": on}) + "\n")
        except OSError:
            pass

    def since(self, seconds: float) -> list[tuple[float, str, bool]]:
        cut = time.time() - seconds
        return [e for e in self.history if e[0] >= cut]

    # -- room --------------------------------------------------------------

    def handles(self) -> set[str]:
        return {c.handle for c in self.clients}

    def new_handle(self) -> str:
        taken = self.handles()
        for _ in range(200):
            h = f"{random.choice(ADJECTIVES)}_{random.choice(NOUNS)}"
            if h not in taken:
                return h
        return f"soul_{random.randint(1000, 9999)}"

    def log(self, text: str, kind: str = "info") -> None:
        self.feed.append((time.time(), text, kind))

    def flip(self, c: Client) -> None:
        now = time.time()
        if now - c.last_flip < self.FLIP_COOLDOWN:
            return
        c.last_flip = now
        self.on = not self.on
        self.flips += 1
        self.last_flipper = c.handle
        self.last_flip_at = now
        self.log(f"{c.handle} turned the lamp {'ON' if self.on else 'off'}",
                 "on" if self.on else "off")
        self.history.append((now, c.handle, self.on))
        self._append_log(now, c.handle, self.on)
        self._save()
        self.broadcast()

    def broadcast(self) -> None:
        for c in list(self.clients):
            c.send(self.screen(c))

    # -- drawing -----------------------------------------------------------

    def screen(self, c: Client) -> str:
        render = {"help": self.help_rows, "log": self.log_rows}.get(c.mode, self.lamp_rows)
        rows = render(c)
        out = ["\x1b[H"]
        for i, row in enumerate(rows):
            out.append(row + "\x1b[K")
            if i != len(rows) - 1:
                out.append("\r\n")
        return "".join(out)

    def _bar(self, kind: str) -> str:
        ch = {"top": ("╔", "╗"), "mid": ("╠", "╣"), "bot": ("╚", "╝")}[kind]
        return f"{FRAME}{ch[0]}{'═' * INNER}{ch[1]}{RESET}"

    def _row(self, content: str) -> str:
        return f"{FRAME}║{RESET}{pad(content)}{FRAME}║{RESET}"

    def lamp_rows(self, c: Client) -> list[str]:
        rows = [self._bar("top")]

        # title
        title = (f"  {BOLD}{WHITE}P I N G L A M P{RESET}  "
                 f"{DYELLOW}▓▒░{RESET} {GREY}a shared bulb on a socket{RESET} "
                 f"{DYELLOW}░▒▓{RESET}")
        right = f"{GREY}you:{RESET} {BOLD}{WHITE}{c.handle}{RESET} "
        rows.append(self._row(title + " " * max(0, INNER - vlen(title) - vlen(right)) + right))
        rows.append(self._bar("mid"))

        # the lamp itself
        art = ART_ON if self.on else ART_OFF
        for i, line in enumerate(block(art)):
            if not self.on:
                colour = GREY if i < 8 else DIM + WHITE
            elif i < 2:
                colour = DYELLOW           # rays
            elif i < 8:
                colour = BOLD + YELLOW     # glass
            else:
                colour = GREY              # fitting
            rows.append(self._row(f"{colour}{line}{RESET}"))

        # verdict
        if self.on:
            banner = f"{BOLD}{YELLOW}>>>{REV}   THE LAMP IS ON   {RESET}{BOLD}{YELLOW}<<<{RESET}"
        else:
            banner = f"{GREY}···   {DIM}the lamp is off{RESET}{GREY}   ···{RESET}"
        rows.append(self._row(centre(banner)))
        rows.append(self._bar("mid"))

        # stats
        if self.last_flipper:
            when = ago(time.time() - self.last_flip_at) if self.last_flip_at else "earlier"
            last = f"{GREY}last flip{RESET} {WHITE}{self.last_flipper}{RESET} {GREY}{when}{RESET}"
        else:
            last = f"{GREY}last flip {DIM}nobody yet{RESET}"
        mid = f"{GREY}flips{RESET} {MAGENTA}{self.flips}{RESET}"
        up = f"{GREY}up{RESET} {GREEN}{clock(time.time() - self.started)}{RESET}  "
        gap1 = max(1, 44 - vlen(last))
        gap2 = max(1, INNER - vlen(last) - gap1 - vlen(mid) - vlen(up))
        rows.append(self._row(" " + last + " " * gap1 + mid + " " * gap2 + up))

        # who is here
        names = []
        for other in self.clients:
            if other is c:
                names.append(f"{BOLD}{WHITE}{other.handle}{GREY}*{RESET}")
            else:
                names.append(f"{GREY}{other.handle}{RESET}")
        souls = f" {GREY}souls{RESET} {CYAN}{len(self.clients)}{RESET} {GREY}»{RESET} "
        joined, room = [], INNER - vlen(souls) - 10
        for n in names:
            if sum(vlen(x) + 2 for x in joined) + vlen(n) > room:
                joined.append(f"{GREY}+{len(names) - len(joined)} more{RESET}")
                break
            joined.append(n)
        rows.append(self._row(souls + f"{GREY},{RESET} ".join(joined)))
        rows.append(self._bar("mid"))

        # the wire
        entries = list(self.feed)
        for _ in range(3 - len(entries)):
            rows.append(self._row(""))
        for ts, text, kind in entries:
            mark = {"on": f"{YELLOW}▲{RESET}", "off": f"{BLUE}▼{RESET}"}.get(kind, f"{GREY}·{RESET}")
            stamp = time.strftime("%H:%M:%S", time.localtime(ts))
            tone = WHITE if kind in ("on", "off") else GREY
            rows.append(self._row(f" {GREY}[{stamp}]{RESET} {mark} {tone}{text}{RESET}"))
        rows.append(self._bar("mid"))

        # keys
        keys = (f" {WHITE}[SPACE]{RESET}{GREY} flip   {WHITE}[L]{GREY} log   "
                f"{WHITE}[N]{GREY} new handle   {WHITE}[H]{GREY} help   "
                f"{WHITE}[Q]{GREY} quit{RESET}")
        port = f"{GREY}port {self.port}{RESET} "
        rows.append(self._row(keys + " " * max(1, INNER - vlen(keys) - vlen(port)) + port))
        rows.append(self._bar("bot"))
        return rows

    def help_rows(self, c: Client) -> list[str]:
        body = [
            f"  {BOLD}{WHITE}WHAT THIS IS{RESET}",
            f"  {GREY}One boolean, living in one server's memory. You are looking at it.{RESET}",
            f"  {GREY}Anyone connected can flip it; everyone connected sees it flip at{RESET}",
            f"  {GREY}the speed of the socket. There are no accounts and nothing saved{RESET}",
            f"  {GREY}about you. Your handle is dice, not identity.{RESET}",
            "",
            f"  {BOLD}{WHITE}KEYS{RESET}",
            f"  {WHITE}SPACE{RESET} {GREY}or{RESET} {WHITE}ENTER{RESET}   {GREY}flip the lamp{RESET}",
            f"  {WHITE}L{RESET}               {GREY}the log: every flip, when, and by whom{RESET}",
            f"  {WHITE}N{RESET}               {GREY}roll a new handle{RESET}",
            f"  {WHITE}H{RESET}               {GREY}this page (any key returns){RESET}",
            f"  {WHITE}Q{RESET} {GREY}or{RESET} {WHITE}CTRL-C{RESET}    {GREY}hang up{RESET}",
            "",
            f"  {BOLD}{WHITE}THE WIRE{RESET}",
            f"  {GREY}Plain TCP. The server negotiates telnet character mode, then{RESET}",
            f"  {GREY}pushes a fresh 24-row ANSI frame to every listener the moment{RESET}",
            f"  {GREY}the state changes. TCP_NODELAY is on: a flip leaves at once.{RESET}",
            "",
            f"  {DYELLOW}pinglamp{RESET} {GREY}· stdlib python · no deps · press any key{RESET}",
            "",
        ]
        rows = [self._bar("top"),
                self._row(f"  {BOLD}{WHITE}P I N G L A M P{RESET}  {GREY}· help ·{RESET}"),
                self._bar("mid")]
        rows.extend(self._row(line) for line in body[:ROWS - 4])
        while len(rows) < ROWS - 1:
            rows.append(self._row(""))
        rows.append(self._bar("bot"))
        return rows

    def sparkline(self, hours: int = 24) -> str:
        """One cell per hour, oldest on the left, so you can see *when*."""
        blocks = "▁▂▃▄▅▆▇█"
        now = time.time()
        buckets = [0] * hours
        for ts, _who, _on in self.history:
            slot = int((now - ts) // 3600)
            if 0 <= slot < hours:
                buckets[hours - 1 - slot] += 1
        peak = max(buckets)
        cells = []
        for n in buckets:
            if n == 0:
                cells.append(f"{GREY}·{RESET}")
            else:
                cells.append(f"{YELLOW}{blocks[min(7, int(n / peak * 7.999))]}{RESET}")
        return "".join(cells)

    def log_rows(self, c: Client) -> list[str]:
        day = self.since(86400)
        who = len({w for _t, w, _o in day})
        rows = [self._bar("top")]

        title = f"  {BOLD}{WHITE}P I N G L A M P{RESET}  {GREY}· the log ·{RESET}"
        summary = (f"{GREY}last 24h{RESET} {MAGENTA}{len(day)}{RESET}{GREY} flips, "
                   f"{RESET}{CYAN}{who}{RESET}{GREY} souls{RESET}  ")
        rows.append(self._row(title + " " * max(1, INNER - vlen(title) - vlen(summary)) + summary))
        rows.append(self._bar("mid"))
        # column starts, matched to the entry rows below: 3, 20, 40, 56
        rows.append(self._row(f"  {GREY}when{' ' * 13}who{' ' * 17}what{' ' * 12}ago{RESET}"))

        recent = list(self.history)[-15:][::-1]        # newest first
        for ts, handle, on in recent:
            stamp = time.strftime("%a %H:%M:%S", time.localtime(ts))
            mark = f"{YELLOW}▲{RESET}" if on else f"{BLUE}▼{RESET}"
            what = f"{WHITE}turned it ON{RESET}" if on else f"{GREY}turned it off{RESET}"
            rows.append(self._row(
                f"  {GREY}{stamp}{RESET}  {mark}  {WHITE}{handle}{RESET}"
                f"{' ' * max(1, 20 - len(handle))}{what}"
                f"{' ' * max(1, 16 - vlen(what))}{GREY}{ago(time.time() - ts)}{RESET}"))
        if not recent:
            rows.append(self._row(f"  {DIM}{GREY}nothing yet. the lamp has never been touched.{RESET}"))
        while len(rows) < 19:                          # keep the pane exactly 15 tall
            rows.append(self._row(""))

        rows.append(self._bar("mid"))
        spark = f"  {GREY}24h ago{RESET} {self.sparkline()} {GREY}now{RESET}"
        hint = f"{GREY}one cell = one hour{RESET}  "
        rows.append(self._row(spark + " " * max(2, INNER - vlen(spark) - vlen(hint)) + hint))
        rows.append(self._bar("mid"))
        rows.append(self._row(f" {GREY}any key returns to the lamp{RESET}"))
        rows.append(self._bar("bot"))
        return rows

    # -- connections -------------------------------------------------------

    async def handle_client(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

        if len(self.clients) >= self.maxclients:
            writer.write(f"\r\n{RED}the lamp is crowded ({self.maxclients} souls). "
                         f"try again shortly.{RESET}\r\n".encode())
            await writer.drain()
            writer.close()
            return

        c = Client(writer, self.new_handle())
        self.clients.append(c)

        writer.write(NEGOTIATE)
        c.send(HIDE_CURSOR + CLEAR)
        self.log(f"{c.handle} joined")
        self.broadcast()

        prev = 0
        try:
            while True:
                data = await reader.read(256)
                if not data:
                    break
                for b in c.decode(data):
                    if b == 10 and prev == 13:   # CRLF is one Enter, not two
                        prev = b
                        continue
                    prev = b
                    if not self.on_key(c, b):
                        return
        except (ConnectionError, asyncio.CancelledError, TimeoutError):
            pass
        finally:
            if c in self.clients:
                self.clients.remove(c)
                self.log(f"{c.handle} left")
                self.broadcast()
            try:
                writer.close()
            except (ConnectionError, RuntimeError):
                pass

    def on_key(self, c: Client, b: int) -> bool:
        """Return False to hang up."""
        if c.mode != "lamp":                      # any key leaves help/log
            c.mode = "lamp"
            c.send(CLEAR + self.screen(c))
            return True

        key = chr(b).lower() if 32 <= b < 127 else ""
        if b in (3, 4) or key == "q":            # ctrl-c, ctrl-d, q
            self.farewell(c)
            return False
        if b in (13, 10) or key == " ":
            self.flip(c)
        elif key == "h" or b == 63:              # h or ?
            c.mode = "help"
            c.send(CLEAR + self.screen(c))
        elif key == "l":
            c.mode = "log"
            c.send(CLEAR + self.screen(c))
        elif key == "n":
            old = c.handle
            c.handle = self.new_handle()
            self.log(f"{old} is now {c.handle}")
            self.broadcast()
        return True

    def farewell(self, c: Client) -> None:
        state = (f"{BOLD}{YELLOW}ON{RESET}" if self.on else f"{GREY}off{RESET}")
        c.send(CLEAR + SHOW_CURSOR +
               f"\r\n  {DYELLOW}▓▒░{RESET} you left the lamp {state}{RESET}"
               f"{GREY} after {clock(time.time() - c.joined)}.{RESET}\r\n"
               f"  {GREY}it keeps burning without you. {RESET}\r\n\r\n")
        try:
            c.writer.close()
        except (ConnectionError, RuntimeError):
            pass

    # -- loops -------------------------------------------------------------

    async def ticker(self) -> None:
        """Keep 'up' and '4s ago' honest for anyone just watching."""
        while True:
            await asyncio.sleep(1.0)
            if self.clients:
                self.broadcast()

    async def serve(self) -> None:
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        addrs = ", ".join(str(s.getsockname()[1]) for s in server.sockets)
        print(f"pinglamp listening on {self.host}:{addrs}  "
              f"(lamp is {'ON' if self.on else 'off'}, {self.flips} flips so far)")
        print(f"  connect:  telnet {self.host if self.host != '0.0.0.0' else 'localhost'} {self.port}")
        tick = asyncio.create_task(self.ticker())
        try:
            async with server:
                await server.serve_forever()
        finally:
            tick.cancel()
            for c in list(self.clients):
                c.send(CLEAR + SHOW_CURSOR + f"\r\n  {RED}the lamp went dark. server closed.{RESET}\r\n")
                try:
                    c.writer.close()
                except (ConnectionError, RuntimeError):
                    pass
            self._save()


def main() -> None:
    ap = argparse.ArgumentParser(description="pinglamp - a shared lamp on a TCP socket")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=2323)
    ap.add_argument("--state", default="lamp.state",
                    help="file to remember the lamp across restarts ('' to forget)")
    ap.add_argument("--log", default="lamp.log",
                    help="append every flip here, for the [L] pane ('' to keep no history)")
    ap.add_argument("--max-clients", type=int, default=64)
    args = ap.parse_args()

    lamp = PingLamp(args.host, args.port, args.state or None, args.log or None,
                    args.max_clients)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task = loop.create_task(lamp.serve())
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, main_task.cancel)
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        print("\npinglamp out.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
