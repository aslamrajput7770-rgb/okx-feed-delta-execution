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
            rc = capture.main(["--venue", "delta", "--minutes", "0.01",
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
