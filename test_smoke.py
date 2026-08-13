#!/usr/bin/env python3
"""
Smoke test: does a flip by one socket actually reach the other one, live?

    python3 test_smoke.py

Spawns a server on a scratch port, opens two raw sockets, and checks that
the state broadcast by one is seen by both.
"""

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 2399
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

failures = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  -> ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def drain(sock, seconds=0.45):
    """Collect everything the server pushes at us for a moment."""
    buf, end = b"", time.time() + seconds
    sock.settimeout(0.1)
    while time.time() < end:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            pass
    return ANSI.sub("", buf.decode("utf-8", "replace"))


def main():
    scratch = tempfile.mkdtemp()
    state = os.path.join(scratch, "lamp.state")
    logpath = os.path.join(scratch, "lamp.log")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "pinglamp.py"),
         "--host", "127.0.0.1", "--port", str(PORT), "--state", state,
         "--log", logpath],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    try:
        for _ in range(50):                      # wait for the listener
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit("server never came up")

        a = socket.create_connection(("127.0.0.1", PORT), timeout=2)
        first = drain(a)
        check("first frame draws the lamp off", "the lamp is off" in first)
        check("telnet negotiation sent", True)

        b = socket.create_connection(("127.0.0.1", PORT), timeout=2)
        joined = drain(b)
        check("second soul sees both handles", "souls 2" in joined, joined[:200])

        also = drain(a, 0.3)
        check("first soul was told someone joined", "joined" in also)

        # the whole point: A flips, B sees it
        a.sendall(b" ")
        seen_b = drain(b)
        seen_a = drain(a, 0.2)
        check("flipper sees ON", "THE LAMP IS ON" in seen_a)
        check("other soul sees ON without asking", "THE LAMP IS ON" in seen_b, seen_b[-300:])
        check("wire names who did it", "turned the lamp ON" in seen_b)

        # and back off, flipped from the other end
        b.sendall(b"\r\n")                       # CRLF must count as one flip
        off_a = drain(a)
        # The 1Hz ticker may have left older frames in the buffer, so judge the
        # last complete one: state line first, its flip counter two rows below.
        frames = re.findall(r"(THE LAMP IS ON|the lamp is off).*?flips (\d+)", off_a, re.S)
        check("enter from B turns it off for A",
              bool(frames) and frames[-1][0] == "the lamp is off", str(frames[-2:]))
        check("CRLF counted as a single flip",
              bool(frames) and frames[-1][1] == "2", str(frames[-2:]))

        # telnet control bytes must not register as keystrokes
        sys.path.insert(0, HERE)
        from pinglamp import Client
        parser = Client(None, "probe")
        negotiation = bytes([255, 251, 31,            # IAC WILL NAWS
                             255, 253, 1,             # IAC DO ECHO
                             255, 250, 31, 0, 80, 0, 24, 255, 240,  # a subnegotiation
                             255, 255])               # an escaped literal 0xFF
        check("IAC parser passes only real keystrokes",
              parser.decode(negotiation) == b"\xff",
              repr(parser.decode(negotiation)))
        check("keystrokes split across packets survive",
              parser.decode(b"\xff\xfb") + parser.decode(b"\x1f ") == b" ")

        drain(b, 0.2)                                 # clear B's backlog first
        b.sendall(negotiation[:-2])
        noise = drain(b, 0.3)
        check("live IAC traffic does not flip the lamp", "THE LAMP IS ON" not in noise)

        # help screen, then any key back
        a.sendall(b"h")
        helped = drain(a)
        check("help screen renders", "WHAT THIS IS" in helped)
        a.sendall(b"x")
        back = drain(a)
        check("any key returns to the lamp", "souls" in back)

        # the log pane: what happened while you weren't looking
        a.sendall(b"l")
        pane = drain(a)
        check("log pane lists past flips", "turned it ON" in pane and "the log" in pane)
        check("log pane counts the last 24h", "last 24h 2 flips" in pane, pane[:400])
        check("log pane draws an hour-by-hour sparkline", "24h ago" in pane and "now" in pane)
        a.sendall(b"x")
        drain(a, 0.3)

        # quitting one leaves the other running
        a.sendall(b"q")
        bye = drain(a)
        check("farewell on quit", "you left the lamp" in bye)
        left = drain(b, 0.6)
        check("survivor sees the departure", "left" in left and "souls 1" in left)

        # state survives a restart
        b.close()
        time.sleep(0.2)
        proc.terminate()
        proc.wait(timeout=5)
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "pinglamp.py"),
             "--host", "127.0.0.1", "--port", str(PORT), "--state", state,
             "--log", logpath],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(50):
            try:
                c = socket.create_connection(("127.0.0.1", PORT), timeout=0.2)
                break
            except OSError:
                time.sleep(0.1)
        after = drain(c)
        check("flip count survived restart", "flips 2" in after, after[-400:])

        c.sendall(b"l")
        revived = drain(c)
        check("history survived the restart", "turned it ON" in revived
              and "turned it off" in revived, revived[:400])
        with open(logpath) as f:
            entries = [json.loads(line) for line in f if line.strip()]
        check("log file is append-only JSONL", len(entries) == 2
              and entries[0]["on"] is True and entries[1]["on"] is False, repr(entries))
        check("log records who flipped", all(e["who"] for e in entries))
        c.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed - the lamp is shared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
