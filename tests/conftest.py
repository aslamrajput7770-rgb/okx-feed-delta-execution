"""Every test runs with its working directory inside `tmp_path`.

`--risk-state` defaults to the relative `state/daily_pnl.json`, which is right
for the service: systemd sets `WorkingDirectory` to the install root, so the
counter lands beside the code and survives a redeploy.

It is wrong for a test. `main()` writes that file whenever it gets far enough to
build a supervisor, which a dry run and a replay both do, so the suite left a
`state/daily_pnl.json` in whatever directory pytest was started from. The
installer runs the suite as root, so the file it left in the install tree was
owned by root - and the service, which runs as an unprivileged user, could not
write its own daily counter. It failed on the first run with a PermissionError.

Chdir-ing for every test fixes the class of bug rather than this one instance:
no test can write into the repository by accident.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
