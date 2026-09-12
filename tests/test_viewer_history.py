#!/usr/bin/env python3
"""Drives the viewer's history features without a human: the H key shows
the rail, a click on the rail loads that revision in a thread and the map
swaps when it is done, a click on HEAD's tick returns, Shift-click sets the
compare revision and turns the Changes lens on with counts that match
atlas_history.changes_between, C cycles the colour lens, and the lens
uniform is only non-zero when there is something to show. Needs an atlas
with history.npz (default data/makepad-draw_atlas). Exit code non-zero on
failure.

    ./tests/test_viewer_history.py [data/makepad-draw_atlas]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import glfw  # noqa: E402
import numpy as np  # noqa: E402
import atlas_history  # noqa: E402
import atlas_viewer as av  # noqa: E402

failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def frames(v, n, dt=1 / 60):
    for _ in range(n):
        v.frame(dt)


def click(v, w, sx, sy, shift=False):
    """A press and release at device pixels (sx, sy)."""
    v.cursor_pt = (sx / v.px, sy / v.px)
    mods = glfw.MOD_SHIFT if shift else 0
    v.on_mouse_button(w, glfw.MOUSE_BUTTON_LEFT, glfw.PRESS, mods)
    v.on_mouse_button(w, glfw.MOUSE_BUTTON_LEFT, glfw.RELEASE, mods)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("atlas", nargs="?", default="data/makepad-draw_atlas")
    args = ap.parse_args()
    if atlas_history.load(args.atlas) is None:
        sys.exit(f"error: {args.atlas} has no history.npz; run ./atlas_history.py {args.atlas}")
    ns = argparse.Namespace(atlas=args.atlas, font=av.atlas_font.DEFAULT_FONT, font_index=0,
                            vector_text=False, frames=None, screenshot=None, goto=None,
                            zoom=None, filter=None, step=None, shots=None, stats=False,
                            color=None, since=None, history=False, rev=None, compare=None)
    v = av.Viewer(ns)
    w = v.win
    glfw.poll_events()
    frames(v, 2)
    h = v.hist
    n = len(h["rev_time"])
    check(v.rail_rect is None and v.lens_code() == 0, "no rail and no lens at start")

    v.on_key(w, glfw.KEY_H, 0, glfw.PRESS, 0)
    frames(v, 2)
    check(v.rail_rect is not None and len(v.rail_xs) == n, f"H shows the rail with {n} ticks")

    # click the tick of the revision 50 back: a thread indexes it, the map swaps
    back = min(50, n - 1)
    head_files = v.n_files
    ry = (v.rail_rect[1] + v.rail_rect[3]) / 2
    t0 = time.perf_counter()
    click(v, w, float(v.rail_xs[back]), ry)
    check(v.rev_loading == back or v.rev_i == back, "a rail click starts loading that revision")
    while v.rev_i != back and time.perf_counter() - t0 < 60:
        frames(v, 1)
    dt = time.perf_counter() - t0
    check(v.rev_i == back, f"revision {h['json']['revisions'][back]['short']} loaded in {dt:.1f} s")
    check(v.res is None and v.metrics == ["tokens"], "a past revision has no resolver and only the tokens layout")
    check(len(v.paths) == v.n_files and v.file_u.shape[0] * v.file_u.shape[1] >= v.n_files,
          f"the corpus swapped: {v.n_files} files (HEAD has {head_files})")
    check((v.rev_dir(back) / "layout.npz").exists(), "the revision's atlas is cached on disk")
    frames(v, 3)

    # back to HEAD by clicking its tick
    click(v, w, float(v.rail_xs[0]), ry)
    frames(v, 2)
    check(v.rev_i == 0 and v.n_files == head_files and v.res is not None, "HEAD's tick returns to the atlas on disk")

    # Shift-click sets the compare revision and turns the Changes lens on
    click(v, w, float(v.rail_xs[back]), ry, shift=True)
    frames(v, 2)
    check(v.compare_i == back and v.color == "changes" and v.lens_code() == 3, "Shift-click sets the compare revision")
    cls, val, rem = atlas_history.changes_between(h, 0, back, n_lines=np.diff(v.a["file_line0"]))
    want = (int((cls == 1).sum()), int((cls == 2).sum()), len(rem))
    check(v.change_counts == want, f"change counts {v.change_counts} match changes_between {want}")
    check((v.lens_c == cls).all() and np.allclose(v.lens_v, val), "lens channels carry the classes and values")

    # C cycles: changes -> none -> churn -> age
    v.on_key(w, glfw.KEY_C, 0, glfw.PRESS, 0)
    check(v.color == "none" and v.lens_code() == 0, "C after changes is none")
    v.on_key(w, glfw.KEY_C, 0, glfw.PRESS, 0)
    frames(v, 1)
    check(v.color == "churn" and v.lens_code() == 1 and v.lens_v.max() > 0.99, "C then churn, hottest file at 1")
    v.on_key(w, glfw.KEY_C, 0, glfw.PRESS, 0)
    check(v.color == "age" and 0 < v.lens_v.min() <= v.lens_v.max() == 1.0, "C then age, ranks in (0, 1]")

    # Left steps the loaded revision one commit older while the rail shows
    v.on_key(w, glfw.KEY_LEFT, 0, glfw.PRESS, 0)
    t0 = time.perf_counter()
    while v.rev_i != 1 and time.perf_counter() - t0 < 60:
        frames(v, 1)
    check(v.rev_i == 1, "Left loads the revision one commit older")
    click(v, w, float(v.rail_xs[0]), ry)
    frames(v, 2)
    check(v.rev_i == 0, "and HEAD's tick returns again")

    glfw.terminate()
    print(f"{len(failures)} failures")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
