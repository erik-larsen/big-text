#!/usr/bin/env python3
"""Self-check for atlas_history.py: builds a throwaway git repository with
four commits, indexes it, reads its history, and checks the CSR, the
per-file sums and first/last commits, the churn window, and the change
classification between revisions against known values.

  ./tests/test_history.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import atlas_history as ah  # noqa: E402


def git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def commit(repo, msg, t):
    env = dict(os.environ, GIT_AUTHOR_DATE=f"{t} +0000", GIT_COMMITTER_DATE=f"{t} +0000",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg, env=env)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        src = repo / "src"
        src.mkdir(parents=True)
        git(repo, "init", "-q")
        t0 = 1_700_000_000
        # c3 (oldest): a.py 3 lines, b.py 2 lines, gone.py 1 line
        (src / "a.py").write_text("x = 1\ny = 2\nz = 3\n")
        (src / "b.py").write_text("p = 1\nq = 2\n")
        (src / "gone.py").write_text("g = 1\n")
        commit(repo, "c3", t0)
        # c2: a.py +1 line, gone.py deleted, c.py added (2 lines)
        (src / "a.py").write_text("x = 1\ny = 2\nz = 3\nw = 4\n")
        (src / "gone.py").unlink()
        (src / "c.py").write_text("c = 1\nd = 2\n")
        commit(repo, "c2", t0 + 100)
        # c1: b.py one line replaced
        (src / "b.py").write_text("p = 1\nq = 3\n")
        commit(repo, "c1", t0 + 200)
        # c0 (newest): empty.py added, temp.py added; then temp.py deleted in the same range later? keep simple
        (src / "empty.py").write_text("")
        commit(repo, "c0", t0 + 300)

        out = Path(tmp) / "atlas"
        subprocess.run([sys.executable, str(HERE / "atlas_index.py"), str(src), "--out", str(out), "--workers", "1"],
                       check=True, capture_output=True)
        st = ah.build(str(out))
        h = ah.load(str(out))
        files = json.load(open(out / "index.json"))["files"]
        idx = {f["path"]: i for i, f in enumerate(files)}
        assert st["revisions"] == 4, st
        assert h["json"]["revisions"][0]["subject"] == "c0"
        assert h["json"]["revisions"][3]["subject"] == "c3"
        a, b, c, e = idx["a.py"], idx["b.py"], idx["c.py"], idx["empty.py"]
        assert h["file_commits"][a] == 2 and h["file_added"][a] == 4 and h["file_removed"][a] == 0
        assert h["file_commits"][b] == 2 and h["file_added"][b] == 3 and h["file_removed"][b] == 1
        assert h["file_commits"][c] == 1 and h["file_first_rev"][c] == 2 and h["file_last_rev"][c] == 2
        assert h["file_first"][a] == t0 and h["file_last"][a] == t0 + 100
        assert h["file_first_rev"][a] == 3 and h["file_last_rev"][a] == 2
        assert "gone.py" in h["json"]["unindexed"]
        # churn window: the last 150 s from HEAD covers c0 and c1 only
        w = ah.churn(h, since=150)
        assert w[b] == 2 and w[a] == 0 and w[c] == 0, w
        assert ah.churn(h)[a] == 4
        # changes between HEAD (0) and c3 (3): a.py, b.py changed; c.py, empty.py added; gone.py removed
        cls, val, rem = ah.changes_between(h, 0, 3, n_lines=[f["lines"] for f in files])
        assert cls[a] == 2 and cls[b] == 2 and cls[c] == 1 and cls[e] == 1, cls
        assert abs(val[a] - 1 / 4) < 1e-9 and abs(val[b] - 2 / 2) < 1e-9, val
        assert [h["json"]["unindexed"][o] for o, _ in rem] == ["gone.py"], rem
        # between HEAD and c2 (2): only b.py changed and empty.py added; c.py existed at c2
        cls, val, rem = ah.changes_between(h, 0, 2, n_lines=[f["lines"] for f in files])
        assert cls[a] == 0 and cls[b] == 2 and cls[c] == 0 and cls[e] == 1 and rem == [], (cls, rem)
        # the order of the two revisions does not matter
        cls2, _, _ = ah.changes_between(h, 2, 0, n_lines=[f["lines"] for f in files])
        assert (cls2 == cls).all()
        print(f"ok: {st['revisions']} revisions, {len(files)} files, churn and changes as expected")


if __name__ == "__main__":
    main()
