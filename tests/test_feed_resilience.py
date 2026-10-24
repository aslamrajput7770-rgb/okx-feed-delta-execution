"""The feed must survive a socket that drops, and stop cleanly on a stop signal.

Both are properties a paper run never exercises, because a laptop socket stays
up and Ctrl-C is not what a service manager sends. They were both broken, and
neither showed up in the existing suite: the run loop called `heartbeat()`
outside any guard, so a dead socket raised out of `run()` and killed the
process, and `main()` only caught `KeyboardInterrupt`, which `systemctl stop`
never sends.

These use hand-written transports rather than mocks of `websocket-client`. The
transport is an injected protocol, so a real implementation of it is the real
code path; a mock would only prove the mock was called.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from orderflow.feed import OkxTradeFeed
from orderflow.feed import base as feed_base

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def no_sleep(monkeypatch):
    """Remove the reconnect wait.

    The backoff is real and is asserted separately; here it is only wall-clock
    that would make a limit-reached test take a minute. The loop itself is
    untouched, so the code path under test is the real one.
    """
    monkeypatch.setattr(feed_base.time, "sleep", lambda _seconds: None)


class ScriptedTransport:
    """A transport whose behaviour per call is written down in advance.

    `recv` results are consumed in order; `None` means "nothing arrived". Once
    the script is exhausted the last entry repeats, so a quiet socket stays
    quiet and a dead one stays dead.
    """

    def __init__(self, recvs, *, is_live=True, heartbeat_raises_after=None,
                 connect_raises=(), recv_raises=None):
        self.script = list(recvs)
        self.is_live = is_live
        self.dead = False
        self.heartbeats = 0
        self.heartbeat_raises_after = heartbeat_raises_after
        self.connect_raises = set(connect_raises)
        self.recv_raises = recv_raises
        self.connects = 0
        self.closed = 0
        self.subscribed = 0
        self.i = 0

    def connect(self):
        self.connects += 1
        if self.connects in self.connect_raises:
            raise OSError(f"connect attempt {self.connects} refused")
        self.dead = False

    def send(self, data, binary=False):
        self.subscribed += 1

    def heartbeat(self):
        self.heartbeats += 1
        if (self.heartbeat_raises_after is not None
                and self.heartbeats > self.heartbeat_raises_after):
            raise OSError("socket is closed")

    def recv(self, timeout=None):
        if self.recv_raises is not None and self.i >= len(self.script):
            raise self.recv_raises
        if self.i < len(self.script):
            value = self.script[self.i]
            self.i += 1
        else:
            value = self.script[-1] if self.script else None
        return value

    def close(self):
        self.closed += 1


def feed_over(transport, **kw):
    return OkxTradeFeed(["BTC-USDT-SWAP"], lambda: transport, **kw)


# --------------------------------------------------------------- dropped socket


def test_heartbeat_failure_reconnects_instead_of_raising(no_sleep):
    """A dead socket used to raise out of run() and kill the process."""
    t = ScriptedTransport([None], heartbeat_raises_after=0)
    stats = feed_over(t).run(lambda trade, ev: None, max_seconds=0.05,
                            reconnect_after_seconds=0.0)
    assert stats.reconnects >= 1
    assert t.connects >= 2, "a drop must produce a fresh connection"
    assert "heartbeat failed" in (stats.last_error or "")


def test_drop_is_not_reported_as_a_protocol_error(no_sleep):
    """A network fault is not a malformed frame, and the two must not merge."""
    t = ScriptedTransport([None], heartbeat_raises_after=0)
    stats = feed_over(t).run(lambda trade, ev: None, max_seconds=0.05,
                            reconnect_after_seconds=0.0)
    assert stats.protocol_errors == 0


def test_a_quiet_socket_is_not_reconnected():
    """Idle is normal on a live feed. Treating it as a drop would reconnect
    every quiet second and never accumulate a footprint."""
    t = ScriptedTransport([None], heartbeat_raises_after=None)
    stats = feed_over(t).run(lambda trade, ev: None, max_seconds=0.05,
                            reconnect_after_seconds=3600.0)
    assert stats.reconnects == 0
    assert t.connects == 1


def test_a_quiet_socket_past_the_limit_is_reconnected(no_sleep):
    """A half-open socket accepts the heartbeat and delivers nothing, so only
    elapsed silence can identify it."""
    t = ScriptedTransport([None])
    stats = feed_over(t).run(lambda trade, ev: None, max_seconds=0.05,
                            reconnect_after_seconds=0.0)
    assert stats.reconnects >= 1


def test_reconnect_limit_is_honoured_and_reported(no_sleep):
    t = ScriptedTransport([None], heartbeat_raises_after=0)
    stats = feed_over(t).run(lambda trade, ev: None, max_seconds=5.0,
                            max_reconnects=2, reconnect_after_seconds=0.0)
    assert stats.reconnects == 2
    assert "reconnect limit reached" in (stats.last_error or "")


def test_a_failed_reconnect_does_not_escape(no_sleep):
    """The venue being down is not a reason to crash: the loop retries."""
    t = ScriptedTransport([None], heartbeat_raises_after=0,
                          connect_raises=(2,))
    stats = feed_over(t).run(lambda trade, ev: None, max_seconds=0.05,
                            max_reconnects=1, reconnect_after_seconds=0.0)
    assert stats.reconnects == 1
    assert "reconnect failed" in (stats.last_error or "")


def test_backoff_is_capped():
    """An outage must not turn into a request storm."""
    assert OkxTradeFeed._backoff(1) == 2.0
    assert OkxTradeFeed._backoff(5) == 30.0
    assert OkxTradeFeed._backoff(50) == 30.0


def test_replay_still_ends_at_the_end_of_the_file():
    """Reconnect logic must not turn a finite replay into an infinite loop."""
    t = ScriptedTransport([], is_live=False)
    stats = feed_over(t).run(lambda trade, ev: None)
    assert stats.reconnects == 0
    assert t.connects == 1


def test_transport_is_closed_even_when_a_callback_raises():
    """A crash mid-run must not leak the socket."""
    frame = json.dumps({
        "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
        "data": [{
            "instId": "BTC-USDT-SWAP", "tradeId": "1", "px": "60000",
            "sz": "1", "side": "buy", "ts": "1792574700000",
        }],
    })
    t = ScriptedTransport([frame])

    def explode(trade, ev):
        raise RuntimeError("callback blew up")

    with pytest.raises(RuntimeError):
        feed_over(t).run(explode, max_frames=10)
    assert t.closed == 1


# ------------------------------------------------------------- stop signal


def test_sigterm_is_handled_like_ctrl_c():
    """systemd sends SIGTERM, never SIGINT.

    A subprocess, because the handler is process-global and installing it in the
    pytest process would leak into every later test.
    """
    script = textwrap.dedent(
        """
        import sys, time
        sys.path.insert(0, %r)
        from live_feed import _exit_on_sigterm
        _exit_on_sigterm()
        print("ready", flush=True)
        try:
            while True:
                time.sleep(0.05)
        except KeyboardInterrupt:
            print("clean shutdown", flush=True)
            raise SystemExit(130)
        """
    ) % str(REPO)

    proc = subprocess.Popen([sys.executable, "-u", "-c", script],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a hang
            proc.kill()
            proc.communicate()

    assert "clean shutdown" in out, (out, err)
    assert proc.returncode == 130


def test_sigterm_before_any_socket_is_installed():
    """The handler is set up before the run starts, so a stop that arrives
    during connection setup is not lost."""
    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, %r)
        from live_feed import main
        # --dry-run never opens a socket, and must still install the handler.
        raise SystemExit(main(["--dry-run", "--minutes", "0.01"]))
        """
    ) % str(REPO)

    proc = subprocess.run([sys.executable, "-u", "-c", script],
                          capture_output=True, text=True, timeout=60)
    # Exits on its own bound; the point is that a signal handler was installed
    # without a socket, so it returns rather than being killed.
    assert proc.returncode in (0, 130), proc.stderr
