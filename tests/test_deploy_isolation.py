"""The suite must not write into the repository it is run from.

The installer runs the suite on the target host, as root. Anything a test leaves
behind in the install tree is therefore owned by root, and the service - which
runs as an unprivileged user - cannot write it. That is how a root-owned
`state/daily_pnl.json` became a PermissionError on the service's first run.

`tests/conftest.py` chdirs every test into a temporary directory. These tests
pin that behaviour, because it is invisible until it breaks a deployment.
"""

from __future__ import annotations

import os
from pathlib import Path

from live_feed import main

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Heavy or generated trees whose contents are not evidence of a leak.
_SKIP = {".git", ".venv", "__pycache__", ".pytest_cache"}


def _tree_snapshot(root: Path) -> dict[str, int]:
    """Path -> mtime for everything under `root` that a test could write."""
    out: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                out[str(p.relative_to(root))] = p.stat().st_mtime_ns
            except OSError:
                continue
    return out


class TestNoWritesIntoTheRepository:
    def test_a_dry_run_leaves_no_state_behind(self, capsys):
        """A dry run still builds a supervisor, so it still saves the counter.

        It must save it somewhere that is not the checkout. The install tree
        legitimately has a `state/` directory - the installer creates it and the
        service writes the counter there - so the check is that this run changed
        nothing under the repository, not that no such file exists.
        """
        before = _tree_snapshot(REPO_ROOT)

        rc = main(["--dry-run", "--order-size", "0.001"])
        capsys.readouterr()
        assert rc == 0

        after = _tree_snapshot(REPO_ROOT)
        added = sorted(set(after) - set(before))
        changed = sorted(p for p in set(after) & set(before) if after[p] != before[p])
        assert not added and not changed, (
            f"the suite wrote into the repository: added={added} changed={changed}. "
            f"conftest.py is not isolating the working directory."
        )

    def test_the_working_directory_is_a_temporary_one(self):
        """The guard above only holds if every test runs somewhere else."""
        assert Path.cwd() != REPO_ROOT
        assert os.access(Path.cwd(), os.W_OK)

    def test_the_counter_lands_beside_the_cwd_not_the_checkout(self, capsys):
        """Why the isolation is needed, stated as observable behaviour.

        The service relies on the path being relative: systemd sets
        WorkingDirectory to the install root, so the counter lands in
        `<install>/state` and survives a redeploy. A test relies on the same
        relativeness pointing somewhere harmless, which is what conftest
        provides. Both facts come from one property, so this asserts it once.
        """
        main(["--dry-run", "--order-size", "0.001"])
        capsys.readouterr()

        here = Path.cwd() / "state" / "daily_pnl.json"
        assert here.exists(), "the counter was not written relative to the cwd"
        assert here.parent.parent != REPO_ROOT, "the counter was written into the checkout"
