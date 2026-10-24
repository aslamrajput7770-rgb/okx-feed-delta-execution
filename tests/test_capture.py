"""The capture tool refuses to let two writers share one tape.

Delta keeps no trade-print history, so a tape cannot be re-fetched: whatever the
recorder wrote is the only copy. Two captures writing the same path is therefore
not a slow-down, it is data loss - the second opens the file "w", truncates the
first one's work, and the two then interleave.

The lock is checked before the socket is opened, which is what lets these tests
run offline.
"""

from __future__ import annotations

import fcntl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import capture  # noqa: E402


class TestOneWriterPerTape:
    def test_a_second_capture_refuses_and_does_not_truncate(self, tmp_path, capsys):
        tape = tmp_path / "t.txt"
        tape.write_text("txt:the first capture's work\n", encoding="utf-8")

        # Stand in for the running capture: hold the same lock it would hold.
        lock = tape.with_suffix(tape.suffix + ".lock")
        holder = lock.open("w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            rc = capture.main(["--minutes", "0.01",
                               "--out", str(tape)])
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

        assert rc == 1
        err = capsys.readouterr().err
        assert "already writing" in err
        # The whole point: the first capture's frames are still there.
        assert tape.read_text(encoding="utf-8") == "txt:the first capture's work\n"

    def test_the_lock_is_released_when_the_holder_goes_away(self, tmp_path):
        """A crashed capture must not leave the tape permanently unwritable."""
        tape = tmp_path / "t.txt"
        lock = tape.with_suffix(tape.suffix + ".lock")
        holder = lock.open("w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        holder.close()  # process exit, effectively: the kernel drops the lock

        again = lock.open("w")
        fcntl.flock(again, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(again, fcntl.LOCK_UN)
        again.close()


class TestTheTapeSpanIsMeasuredNotCounted:
    """A resume must record the rest of the window, not another full one.

    `--minutes 1440` means "cover a day". Counting frames cannot answer how much
    time is covered, because the venue is quiet at some hours and busy at
    others; only the venue's own timestamps can. Getting this wrong doubles the
    requested window, quietly.
    """

    def _tape(self, path, start_us, spans_us):
        lines = []
        for i, span in enumerate(spans_us):
            lines.append(
                'txt:{"p":"80000.0","r":"t","s":1.0,"sy":"BTCUSD",'
                f'"t":{start_us + span},"type":"trades"}}'
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_span_comes_from_the_timestamps(self, tmp_path):
        p = self._tape(tmp_path / "t.txt", 1_792_805_400_000_000,
                       [0, 60_000_000, 3_600_000_000])
        assert capture._tape_seconds(p) == pytest.approx(3600.0)

    def test_an_empty_tape_covers_nothing(self, tmp_path):
        p = tmp_path / "t.txt"
        p.write_text("", encoding="utf-8")
        assert capture._tape_seconds(p) == 0.0

    def test_control_frames_do_not_inflate_the_span(self, tmp_path):
        """Heartbeats carry no `t`, so they cannot be mistaken for prints."""
        p = tmp_path / "t.txt"
        p.write_text(
            'txt:{"type":"heartbeat"}\n'
            'txt:{"p":"80000.0","r":"t","s":1.0,"t":1792805400000000,"type":"trades"}\n'
            'txt:{"type":"subscriptions"}\n'
            'txt:{"p":"80001.0","r":"t","s":1.0,"t":1792805460000000,"type":"trades"}\n',
            encoding="utf-8",
        )
        assert capture._tape_seconds(p) == pytest.approx(60.0)

    def test_a_covered_tape_records_nothing(self, tmp_path, capsys):
        """No socket is opened on this path, which is why it is testable."""
        p = self._tape(tmp_path / "t.txt", 1_792_805_400_000_000,
                       [0, 7_200_000_000])  # 2 h
        rc = capture.main(["--minutes", "60", "--out", str(p)])
        assert rc == 0
        assert "nothing to record" in capsys.readouterr().out
