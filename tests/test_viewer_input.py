#!/usr/bin/env python3
"""Drives atlas_viewer's input callbacks without a human: wheel zoom keeps
the world point under the cursor fixed and glides to a stop, drag pans the
map with the cursor, the filter box takes typed text and finds hits,
stepping flies to a result at the text rung, Escape clears, R refits, and
--goto lands on the file. Exit code non-zero on failure.

    ./tests/test_viewer_input.py [data/synthetic_atlas]
"""
import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import glfw  # noqa: E402
import atlas_viewer as av  # noqa: E402

failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def frames(v, n, dt=1 / 60):
    for _ in range(n):
        v.frame(dt)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("atlas", nargs="?", default="data/synthetic_atlas")
    args = ap.parse_args()
    ns = argparse.Namespace(atlas=args.atlas, font=av.atlas_font.DEFAULT_FONT, font_index=0,
                            vector_text=False, frames=1, screenshot=None, goto=None,
                            zoom=None, filter=None, step=None, shots=None, stats=False)
    v = av.Viewer(ns)
    v.scripted = False                 # take input from the calls below
    glfw.poll_events()                 # show the window once, then no real events
    av.glfw = type("G", (), dict(vars(glfw), poll_events=staticmethod(lambda: None)))
    w = v.win
    frames(v, 2)

    # wheel zoom about the cursor: the world point under it must not move
    cx, cy = 0.3 * v.fb_w / v.px, 0.6 * v.fb_h / v.px      # window points
    v.on_cursor(w, cx, cy)
    before = v.screen_to_world(cx * v.px, cy * v.px)
    z0 = v.zoom
    for _ in range(5):
        v.on_scroll(w, 0, 1.0)
        frames(v, 3)
    frames(v, 60)                     # glide runs out
    after = v.screen_to_world(cx * v.px, cy * v.px)
    check(v.zoom > z0 * 1.5, f"wheel zoomed in ({z0:.3f} -> {v.zoom:.3f})")
    check(abs(after[0] - before[0]) < 1e-6 * v.W and abs(after[1] - before[1]) < 1e-6 * v.H,
          f"world point under the cursor fixed while zooming {before} -> {after}")
    check(v.zoom_vel == 0.0, "glide came to rest")

    # drag pans: the map follows the cursor exactly
    v.on_mouse_button(w, glfw.MOUSE_BUTTON_LEFT, glfw.PRESS, 0)
    p0 = v.screen_to_world(cx * v.px, cy * v.px)
    v.on_cursor(w, cx + 100, cy + 40)
    v.on_mouse_button(w, glfw.MOUSE_BUTTON_LEFT, glfw.RELEASE, 0)
    frames(v, 2)
    p1 = v.screen_to_world((cx + 100) * v.px, (cy + 40) * v.px)
    check(abs(p1[0] - p0[0]) < 1e-9 and abs(p1[1] - p0[1]) < 1e-9,
          f"drag keeps the grabbed point under the cursor {p0} -> {p1}")

    # hover on a file at the text rung gives a label with a line number
    v.fit()
    v.set_zoom_ppl(16.0, (v.W / 2, v.H / 2))
    v.on_cursor(w, v.fb_w / 2 / v.px, v.fb_h / 2 / v.px)
    frames(v, 2)
    lab = v.hover_label()
    check(lab is not None and ":" in lab, f"hover label at text rung: {lab!r}")
    check("←" in v.crumb, f"crumb trail: {v.crumb!r}")

    # filter: '/' focuses, typed chars search in a thread, results arrive
    v.fit()
    word = v.item_names[0]
    v.on_key(w, glfw.KEY_SLASH, 0, glfw.PRESS, 0)
    v.on_char(w, ord("/"))              # the char that follows the focusing key
    check(v.filter_focus and v.filter_text == "", "slash focuses the filter and is not typed")
    for ch in word:
        v.on_char(w, ord(ch))
    t0 = time.perf_counter()
    while (v.results is None or v.results["word"] != word) and time.perf_counter() - t0 < 5:
        frames(v, 1)
    check(v.results is not None and len(v.results["order"]) > 0,
          f"filter {word!r} found {0 if v.results is None else len(v.results['order'])} hits")
    check(bool(v.hits_by_file.any()) and bool(v.dimmed.any()), "hit files flagged, others dimmed")
    check(len(v.panel_rows) > 2, f"results panel has {len(v.panel_rows)} rows")
    n_def = int(v.results["is_def"].sum())
    check(n_def >= 1, f"{n_def} definitions among the hits")

    # a click in the filter box focuses it; a click on a result row flies there
    v.filter_focus = False
    frames(v, 1)
    fx, fy = (v.filter_rect[0] + 5) / v.px, (v.filter_rect[1] + 5) / v.px
    v.on_cursor(w, fx, fy)
    v.on_mouse_button(w, glfw.MOUSE_BUTTON_LEFT, glfw.PRESS, 0)
    v.on_mouse_button(w, glfw.MOUSE_BUTTON_LEFT, glfw.RELEASE, 0)
    check(v.filter_focus, "click in the filter box focuses it")
    row = next(k for k, r in enumerate(v.panel_rows) if r[2] >= 0)
    ry = v.panel_rect[1] + 4 * v.px + (row + 0.5) * 13 * v.px
    v.on_cursor(w, (v.panel_rect[0] + 20) / v.px, ry / v.px)
    v.on_mouse_button(w, glfw.MOUSE_BUTTON_LEFT, glfw.PRESS, 0)
    v.on_mouse_button(w, glfw.MOUSE_BUTTON_LEFT, glfw.RELEASE, 0)
    check(v.result_i == v.panel_rows[row][2] and v.fly is not None,
          f"click on panel row {row} selects result {v.result_i} and flies")
    v.result_i = -1
    v.fly = None
    v.update_file_flags()
    v.fit()

    # Enter steps to the first result and flies there; the fly ends at text rung
    v.on_key(w, glfw.KEY_ENTER, 0, glfw.PRESS, 0)
    check(v.result_i == 0 and v.fly is not None, "Enter steps to result 1 and starts a fly-to")
    t0 = time.perf_counter()
    while v.fly is not None and time.perf_counter() - t0 < 3:
        frames(v, 1)
    f, line, col, ln, is_def, kind = v.result(0)
    x0, y0, x1, y1 = v.view()
    lx, ly = v.a["line_pos"][line]
    check(x0 <= lx <= x1 and y0 <= ly <= y1, "fly-to ended with the hit line in view")
    check(v.rung[f] == 3, f"hit file at the text rung after the fly (rung {v.rung[f]})")
    check(v.current_file == f, "current result file flagged")
    v.on_key(w, glfw.KEY_UP, 0, glfw.PRESS, 0)
    check(v.result_i == len(v.results["order"]) - 1, "Up wraps to the last result")

    # Escape clears the filter, then the selection
    v.on_key(w, glfw.KEY_ESCAPE, 0, glfw.PRESS, 0)
    frames(v, 1)
    check(v.filter_text == "" and v.results is None and not v.dimmed.any(),
          "Escape clears the filter, the results and the dimming")

    # R refits
    v.on_key(w, glfw.KEY_R, 0, glfw.PRESS, 0)
    check(abs(v.zoom - v.fit_zoom()) < 1e-12 and v.cx == v.W / 2, "R refits")

    # --goto path:line lands on the line at the text rung
    path = v.paths[len(v.paths) // 2]
    v.goto(f"{path}:5", complete=True)
    frames(v, 1)
    f = v.paths.index(path)
    x0, y0, x1, y1 = v.view()
    r = v.a["file_rect"][f]
    check(x0 < r[2] and x1 > r[0] and y0 < r[3] and y1 > r[1] and v.rung[f] == 3,
          f"--goto {path}:5 shows the file at rung {v.rung[f]}")

    # zoom limits hold
    v.on_cursor(w, cx, cy)
    for _ in range(200):
        v.on_scroll(w, 0, 5.0)
        frames(v, 1)
    frames(v, 60)
    lo, hi = v.zoom_limits()
    check(abs(v.zoom - hi) < 1e-9 * hi, f"zoom clamps at the maximum ({v.zoom:.2f} vs {hi:.2f})")
    for _ in range(200):
        v.on_scroll(w, 0, -5.0)
        frames(v, 1)
    frames(v, 60)
    check(abs(v.zoom - lo) < 1e-9 * lo, f"zoom clamps at the minimum ({v.zoom:.4f} vs {lo:.4f})")

    glfw.terminate()
    print(f"{len(failures)} failures")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
