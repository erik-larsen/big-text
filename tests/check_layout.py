#!/usr/bin/env python3
"""Check the layout invariants of a data/<name>_atlas (docs/DESIGN.md, Stage 2).

Exit code is non-zero on any failure. Checks: array names, dtypes and shapes
of index.npz and layout.npz; every file rectangle inside its directory's inner
rectangle and every directory inside its parent's; siblings (files and child
directories of one directory) do not overlap; every line cell inside its
file's rectangle; item rectangles inside their file's rectangle; and
file_cols * file_rows >= rows for every file, and the rows of a line cover its characters.

  tests/check_layout.py data/big-picture_atlas
"""
import argparse
import json
import os
import sys

import numpy as np

EPS = 1e-6

INDEX_ARRAYS = {"chars": "uint8", "kinds": "uint8", "line_off": "uint64", "line_file": "uint32",
                "line_indent": "uint16", "line_len": "uint16", "file_line0": "uint32",
                "file_chars": "uint32", "file_dir": "uint32", "item_file": "uint32",
                "item_start": "uint32", "item_end": "uint32", "item_kind": "uint8"}
LAYOUT_ARRAYS = {"world": "float64", "dir_rect": "float64", "dir_pad": "float64",
                 "dir_depth": "uint16", "dir_hue": "uint8", "file_rect": "float64",
                 "file_pitch": "float64", "file_cols": "uint16", "file_rows": "uint32",
                 "file_cap": "uint16", "file_colw": "float64", "file_hue": "uint8",
                 "row_pos": "float32", "row_line": "uint32", "row_col0": "uint16", "row_len": "uint16",
                 "line_row0": "uint32", "file_row0": "uint32",
                 "item_rect": "float32", "item_rect_item": "uint32"}


def inside(inner, outer, eps=EPS):
    """Boolean per row: rect inner lies within rect outer."""
    return ((inner[:, 0] >= outer[:, 0] - eps) & (inner[:, 1] >= outer[:, 1] - eps)
            & (inner[:, 2] <= outer[:, 2] + eps) & (inner[:, 3] <= outer[:, 3] + eps))


def count_overlaps(r, eps=EPS):
    """Number of overlapping pairs among rectangles r (n, 4), by blocks."""
    n, total = len(r), 0
    for i0 in range(0, n, 512):
        a = r[i0:i0 + 512]
        ox = (a[:, None, 0] < r[None, :, 2] - eps) & (r[None, :, 0] < a[:, None, 2] - eps)
        oy = (a[:, None, 1] < r[None, :, 3] - eps) & (r[None, :, 1] < a[:, None, 3] - eps)
        o = ox & oy
        o[np.arange(len(a)), np.arange(i0, i0 + len(a))] = False
        total += int(o.sum())
    return total // 2


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("atlas", metavar="ATLAS_DIR")
    args = ap.parse_args()
    failures = []

    def check(ok, msg):
        print(("ok   " if ok else "FAIL ") + msg)
        if not ok:
            failures.append(msg)

    z = np.load(os.path.join(args.atlas, "index.npz"))
    L = np.load(os.path.join(args.atlas, "layout.npz"))
    with open(os.path.join(args.atlas, "index.json")) as f:
        meta = json.load(f)
    with open(os.path.join(args.atlas, "layout.json")) as f:
        lay = json.load(f)

    for name, dt in INDEX_ARRAYS.items():
        check(name in z.files and str(z[name].dtype) == dt,
              f"index.npz {name} is {dt} (got {str(z[name].dtype) if name in z.files else 'missing'})")
    for name, dt in LAYOUT_ARRAYS.items():
        check(name in L.files and str(L[name].dtype) == dt,
              f"layout.npz {name} is {dt} (got {str(L[name].dtype) if name in L.files else 'missing'})")
    if failures:
        sys.exit(f"{len(failures)} failures")

    dirs = meta["dirs"]
    n_files, n_dirs, n_items = len(meta["files"]), len(dirs), len(meta["items"])
    n_lines = len(z["line_len"])
    file_line0, file_dir = z["file_line0"], z["file_dir"]
    check(len(z["line_off"]) == n_lines + 1 and int(z["line_off"][-1]) == len(z["chars"]) == len(z["kinds"]),
          f"index: line_off spans {len(z['chars'])} chars, {n_lines} lines")
    check(len(file_line0) == n_files + 1 and int(file_line0[-1]) == n_lines and len(file_dir) == n_files,
          f"index: {n_files} files cover the lines")
    check((np.diff(file_line0.astype(np.int64)) >= 0).all() and (np.diff(z["line_off"].astype(np.int64)) >= 0).all(),
          "index: file_line0 and line_off are monotonic")
    check((z["line_len"] == np.diff(z["line_off"]).astype(np.uint16)).all(), "index: line_len matches line_off")
    check(bool((z["line_file"] == np.repeat(np.arange(n_files, dtype=np.uint32),
                                            np.diff(file_line0.astype(np.int64)))).all()),
          "index: line_file matches file_line0")
    check(len(z["item_file"]) == n_items and (z["item_end"] > z["item_start"]).all()
          and (z["item_end"] <= np.diff(file_line0.astype(np.int64))[z["item_file"]]).all(),
          f"index: {n_items} items with non-empty extents inside their files")

    world, dir_rect, dir_pad = L["world"], L["dir_rect"], L["dir_pad"]
    file_rect, pitch = L["file_rect"], L["file_pitch"]
    cols, rows, colw = L["file_cols"].astype(np.int64), L["file_rows"].astype(np.int64), L["file_colw"]
    check(dir_rect.shape == (n_dirs, 4) and file_rect.shape == (n_files, 4)
          and L["row_pos"].shape == (len(L["row_line"]), 2) and len(L["dir_pad"]) == n_dirs
          and len(L["line_row0"]) == n_lines + 1 and len(L["file_row0"]) == n_files + 1
          and len(pitch) == n_files and L["item_rect"].shape[1] == 4
          and len(L["item_rect_item"]) == len(L["item_rect"]) and len(lay["dirs"]) == n_dirs,
          "layout: array shapes match the index")
    check(np.allclose(dir_rect[0], [0, 0, world[0], world[1]]) and list(lay["world"]) == list(world),
          f"layout: root is the world {world[0]:.0f}x{world[1]:.1f}")
    check((dir_rect[:, 2] >= dir_rect[:, 0]).all() and (dir_rect[:, 3] >= dir_rect[:, 1]).all()
          and (file_rect[:, 2] >= file_rect[:, 0]).all() and (file_rect[:, 3] >= file_rect[:, 1]).all(),
          "layout: no inverted rectangles")

    inner = dir_rect + np.stack([dir_pad, dir_pad, -dir_pad, -dir_pad], axis=1)
    check((inner[:, 2] >= inner[:, 0] - EPS).all() and (inner[:, 3] >= inner[:, 1] - EPS).all(),
          "layout: padding leaves a non-negative inner rectangle")
    bad = ~inside(file_rect, inner[file_dir])
    check(not bad.any(), f"layout: every file rectangle inside its directory's inner rectangle "
                         f"({int(bad.sum())} outside)")
    parent = np.array([d["parent"] for d in dirs])
    if n_dirs > 1:
        bad = ~inside(dir_rect[1:], inner[parent[1:]])
        check(not bad.any(), f"layout: every directory inside its parent's inner rectangle "
                             f"({int(bad.sum())} outside)")
    depth = L["dir_depth"].astype(np.int64)
    check(depth[0] == 0 and (n_dirs == 1 or (depth[1:] == depth[parent[1:]] + 1).all()),
          "layout: dir_depth is the tree depth")

    overlaps, siblings = 0, 0
    for d in range(n_dirs):
        r = np.concatenate([file_rect[dirs[d]["files"]], dir_rect[dirs[d]["children"]]])
        if len(r) > 1:
            overlaps += count_overlaps(r)
            siblings += len(r)
    check(overlaps == 0, f"layout: sibling rectangles do not overlap ({overlaps} overlapping pairs "
                         f"among {siblings} siblings)")

    file_row0, line_row0 = L["file_row0"].astype(np.int64), L["line_row0"].astype(np.int64)
    row_line, row_col0, row_len = L["row_line"].astype(np.int64), L["row_col0"].astype(np.int64), L["row_len"].astype(np.int64)
    n_rows = len(row_line)
    check((cols * rows >= np.diff(file_row0)).all() and (cols >= 1).all()
          and (rows >= 1).all(), "layout: file_cols * file_rows >= rows for every file")
    check((pitch > 0).all() and (colw > 0).all(), "layout: positive pitches and column widths")
    check((np.diff(line_row0) >= 1).all() and line_row0[-1] == n_rows
          and (file_row0 == line_row0[file_line0]).all(),
          "layout: every line has at least one row and file_row0 follows line_row0")
    line_len = z["line_len"].astype(np.int64)
    covered = np.zeros(n_lines, np.int64)
    np.add.at(covered, row_line, row_len)
    cap = L["file_cap"].astype(np.int64)[z["line_file"]]
    wrapped = cap >= 16
    check((covered[wrapped] == line_len[wrapped]).all()
          and (covered[~wrapped] == np.minimum(line_len[~wrapped], 4096)).all()
          and (row_col0[np.diff(line_row0)[row_line] == 1] == 0).all(),
          "layout: the rows of a line cover exactly its characters")
    rf = z["line_file"][row_line]
    lp = L["row_pos"].astype(np.float64)
    cell = np.stack([lp[:, 0], lp[:, 1], lp[:, 0], lp[:, 1] + pitch[rf]], axis=1)
    bad = ~inside(cell, file_rect[rf], eps=1e-3)      # row_pos is float32
    check(not bad.any(), f"layout: every row cell inside its file's rectangle ({int(bad.sum())} outside)")
    # the column a row sits in is the one the viewer will compute from x
    c = ((lp[:, 0] - file_rect[rf, 0]) / colw[rf] + 0.5).astype(np.int64)   # rows hang in a little
    j = np.arange(n_rows) - file_row0[:-1][rf]
    check((c == j // rows[rf]).all() and (c < cols[rf]).all(),
          "layout: row x positions land on their column")

    ir, ii = L["item_rect"].astype(np.float64), L["item_rect_item"]
    check(len(ii) == 0 or int(ii.max()) < n_items, "layout: item_rect_item indexes items")
    if len(ii):
        bad = ~inside(ir, file_rect[z["item_file"][ii]], eps=1e-3)
        check(not bad.any(), f"layout: item rectangles inside their file's rectangle ({int(bad.sum())} outside)")
        check((ir[:, 2] > ir[:, 0]).all() and (ir[:, 3] > ir[:, 1]).all(), "layout: item rectangles have area")
    covered = np.zeros(n_items, bool)
    covered[ii] = True
    check(covered.all(), f"layout: every item has at least one rectangle ({int((~covered).sum())} without)")

    print(f"{args.atlas}: {n_files} files, {n_dirs} dirs, {n_lines} lines, {n_items} items, "
          f"{len(ir)} item rects: {len(failures)} failures")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
