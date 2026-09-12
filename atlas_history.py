#!/usr/bin/env python3
"""Git history of an atlas: churn and recency per file, and a per-revision
table of the files each commit touched (phase 5 of docs/PARITY.md).

Reads index.json for the root and the file paths, finds the repository that
contains the root (which may be a submodule), and runs one
`git log --numstat --no-renames` over the indexed directories. Renames are
not followed on purpose: a rename is a removal and an addition, which is
what change lighting can show.

Writes beside the index:

  history.npz
    rev_time      int64  [n_revs]        commit time, newest first
    rev_ptr       int64  [n_revs + 1]    CSR pointer into the entries below
    rev_file      int32  [n_entries]     file index, -1 for a path not in the index
    rev_other     int32  [n_entries]     for those, index into json 'unindexed'; else -1
    rev_added     uint32 [n_entries]     numstat counts ('-' for binaries counts as 0)
    rev_removed   uint32 [n_entries]
    file_commits  uint32 [n_files]       commits touching the file
    file_added    uint32 [n_files]       summed over all commits
    file_removed  uint32 [n_files]
    file_first    int64  [n_files]       time of the first commit touching the file, 0 when untracked
    file_last     int64  [n_files]       time of the last
    file_first_rev int32 [n_files]       index of that first commit in the table (the largest, since newest first), -1 when untracked
    file_last_rev  int32 [n_files]       index of the last (the smallest)

  history.json
    toplevel, prefix ("" or "sub/dir/"), revisions [{hash, short, time, author,
    subject, files}] newest first in npz order, unindexed (paths seen in the
    log that are not in the index: deleted files, binaries, skipped files;
    root-relative like the index's paths),
    stats {revisions, tracked, untracked, seconds}

Usage:
  ./atlas_history.py data/<name>_atlas [--max-revisions N]

The viewer computes everything windowed (churn over the last N days, changes
between two revisions) from the CSR; see changes_between() below, which the
viewer imports.
"""
import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np

FORMAT = "%H%x00%ct%x00%an%x00%s"


def run(args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def pathspecs(meta):
    """Root-relative paths to log. The index does not record which source
    dirs were given: with several, the root has no files of its own and its
    child directories are exactly the source dirs; with one, the root is
    the source dir and is logged whole, so deleted files show up too."""
    root = meta["dirs"][0]
    if root["files"] or not root["children"]:
        return ["."]
    return [meta["dirs"][c]["path"] for c in root["children"]]


def parse_log(text, path_index, prefix=""):
    """The git log output into revisions and CSR arrays, plus the table of
    paths that are not in the index."""
    revs, ptr, files, other, added, removed = [], [0], [], [], [], []
    unindexed = {}
    cur = None
    for line in text.split("\n"):
        if not line:
            continue
        if "\0" in line:
            if cur is not None:
                revs.append(cur)
                ptr.append(len(files))
            h, t, author, subject = line.split("\0", 3)
            cur = {"hash": h, "short": h[:7], "time": int(t), "author": author, "subject": subject, "files": 0}
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3 or cur is None:
            continue
        a, r, path = parts
        fi = path_index.get(path, -1)
        files.append(fi)
        rel = path[len(prefix):] if prefix and path.startswith(prefix) else path
        other.append(-1 if fi >= 0 else unindexed.setdefault(rel, len(unindexed)))
        added.append(0 if a == "-" else int(a))
        removed.append(0 if r == "-" else int(r))
        cur["files"] += 1
    if cur is not None:
        revs.append(cur)
        ptr.append(len(files))
    return (revs, list(unindexed), np.array(ptr, np.int64), np.array(files, np.int32),
            np.array(other, np.int32), np.array(added, np.uint32), np.array(removed, np.uint32))


def build(atlas, max_revisions=None):
    t0 = time.time()
    with open(os.path.join(atlas, "index.json")) as f:
        meta = json.load(f)
    root = meta["root"]
    try:
        toplevel = run(["git", "rev-parse", "--show-toplevel"], cwd=root).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit(f"error: {root} is not inside a git repository")
    prefix = os.path.relpath(os.path.realpath(root), os.path.realpath(toplevel))
    prefix = "" if prefix == "." else prefix + "/"
    path_index = {prefix + f["path"]: i for i, f in enumerate(meta["files"])}
    specs = [prefix + s if s != "." else (prefix or ".") for s in pathspecs(meta)]
    cmd = ["git", "log", "--numstat", "--no-renames", f"--format={FORMAT}"]
    if max_revisions:
        cmd.append(f"-n{max_revisions}")
    text = run(cmd + ["--"] + specs, cwd=toplevel)
    revs, unindexed, rev_ptr, rev_file, rev_other, rev_added, rev_removed = parse_log(text, path_index, prefix)
    # keep only revisions that touch an indexed file (a pathspec can match
    # files the index skipped, such as binaries)
    keep = np.array([(rev_file[rev_ptr[i]:rev_ptr[i + 1]] >= 0).any() for i in range(len(revs))], bool)
    if not keep.all():
        sel = np.concatenate([np.arange(rev_ptr[i], rev_ptr[i + 1]) for i in np.flatnonzero(keep)]) \
            if keep.any() else np.zeros(0, np.int64)
        counts = np.diff(rev_ptr)[keep]
        revs = [r for r, k in zip(revs, keep) if k]
        rev_ptr = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)
        rev_file, rev_other = rev_file[sel], rev_other[sel]
        rev_added, rev_removed = rev_added[sel], rev_removed[sel]
    n_files = len(meta["files"])
    rev_time = np.array([r["time"] for r in revs], np.int64)
    ok = rev_file >= 0
    fi = rev_file[ok]
    entry_rev = np.repeat(np.arange(len(revs)), np.diff(rev_ptr))[ok]
    file_commits = np.bincount(fi, minlength=n_files).astype(np.uint32)
    file_added = np.bincount(fi, weights=rev_added[ok], minlength=n_files).astype(np.uint32)
    file_removed = np.bincount(fi, weights=rev_removed[ok], minlength=n_files).astype(np.uint32)
    file_first = np.zeros(n_files, np.int64)
    file_last = np.zeros(n_files, np.int64)
    file_first_rev = np.full(n_files, -1, np.int32)
    file_last_rev = np.full(n_files, -1, np.int32)
    if len(fi):
        # newest first: the first commit is the largest index, the last the
        # smallest; files never touched stay 0 / -1
        np.maximum.at(file_first_rev, fi, entry_rev.astype(np.int32))
        small = np.full(n_files, len(revs), np.int32)
        np.minimum.at(small, fi, entry_rev.astype(np.int32))
        tracked_mask = file_commits > 0
        file_last_rev[tracked_mask] = small[tracked_mask]
        file_first[tracked_mask] = rev_time[file_first_rev[tracked_mask]]
        file_last[tracked_mask] = rev_time[file_last_rev[tracked_mask]]
    seconds = round(time.time() - t0, 2)
    np.savez(os.path.join(atlas, "history.npz"),
             rev_time=rev_time, rev_ptr=rev_ptr, rev_file=rev_file, rev_other=rev_other, rev_added=rev_added,
             rev_removed=rev_removed, file_commits=file_commits, file_added=file_added,
             file_removed=file_removed, file_first=file_first, file_last=file_last,
             file_first_rev=file_first_rev, file_last_rev=file_last_rev)
    tracked = int((file_commits > 0).sum())
    stats = {"revisions": len(revs), "tracked": tracked, "untracked": n_files - tracked, "seconds": seconds}
    with open(os.path.join(atlas, "history.json"), "w") as f:
        json.dump({"toplevel": toplevel, "prefix": prefix, "pathspecs": specs, "revisions": revs,
                   "unindexed": unindexed, "stats": stats}, f)
    return stats


def load(atlas):
    """(npz dict, json dict) or None when there is no history."""
    p = os.path.join(atlas, "history.npz")
    if not os.path.exists(p):
        return None
    z = np.load(p)
    h = {k: z[k] for k in z.files}
    with open(os.path.join(atlas, "history.json")) as f:
        h["json"] = json.load(f)
    return h


def churn(h, since=None, now=None):
    """Per-file added + removed over the whole history, or over the commits
    newer than now - since seconds."""
    if since is None:
        return h["file_added"].astype(np.float64) + h["file_removed"].astype(np.float64)
    now = h["rev_time"][0] if now is None else now
    n_files = len(h["file_added"])
    n_rev = int(np.searchsorted(-h["rev_time"], -(now - since)))     # newest first
    e = int(h["rev_ptr"][n_rev])
    f = h["rev_file"][:e]
    ok = f >= 0
    w = h["rev_added"][:e][ok].astype(np.float64) + h["rev_removed"][:e][ok]
    return np.bincount(f[ok], weights=w, minlength=n_files)


def changes_between(h, rev_a, rev_b, n_lines=None):
    """Change lighting between two revision indices of the table (newest
    first, so a smaller index is newer). Considers the commits in (older,
    newer]. Returns (cls, value, removed) where cls per file is 0 unchanged,
    1 added (its first commit is inside the range, even if it added no
    lines), 2 changed; value is the fraction of the file's lines touched
    (added + removed over n_lines, clamped to 1; 1.0 when n_lines is None);
    removed is the list of (unindexed path index, revision index), newest
    first, one per path, for paths not in the index whose newest entry in
    the range is a pure deletion and which have an entry older than the
    range, so they existed at the older revision: the files deleted between
    the two revisions (json['unindexed'] gives the path). A path added and
    deleted inside the range is neither."""
    newer, older = min(rev_a, rev_b), max(rev_a, rev_b)
    s, e = int(h["rev_ptr"][newer]), int(h["rev_ptr"][older])
    n_files = len(h["file_added"])
    f = h["rev_file"][s:e]
    ok = f >= 0
    a = h["rev_added"][s:e].astype(np.float64)
    r = h["rev_removed"][s:e].astype(np.float64)
    touched = np.bincount(f[ok], weights=(a + r)[ok], minlength=n_files)
    cls = np.where(touched > 0, 2, 0).astype(np.uint8)
    fr = h["file_first_rev"]
    cls[(fr >= 0) & (fr >= newer) & (fr < older)] = 1
    if n_lines is None:
        value = np.where(touched > 0, 1.0, 0.0)
    else:
        value = np.clip(touched / np.maximum(np.asarray(n_lines, np.float64), 1.0), 0.0, 1.0)
    entry_rev = np.repeat(np.arange(newer, older), np.diff(h["rev_ptr"][newer:older + 1]))
    other = h["rev_other"][s:e]
    before = set(h["rev_other"][e:].tolist())          # paths with an entry older than the range
    removed, seen = [], set()
    for i in np.flatnonzero(~ok):                       # newest first: the first sight of a path decides
        o = int(other[i])
        if o in seen:
            continue
        seen.add(o)
        if r[i] > 0 and a[i] == 0 and o in before:
            removed.append((o, int(entry_rev[i])))
    return cls, value, removed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("atlas", metavar="ATLAS_DIR", help="data/<name>_atlas with index.json")
    ap.add_argument("--max-revisions", type=int, default=None, help="keep only the newest N commits")
    args = ap.parse_args()
    st = build(args.atlas, args.max_revisions)
    print(f"wrote {args.atlas}/history.npz + history.json: {st['revisions']} revisions, "
          f"{st['tracked']} tracked files, {st['untracked']} untracked, {st['seconds']}s")


if __name__ == "__main__":
    main()
