#!/usr/bin/env python3
"""Stage 3b: an atlas as one flat file for the C viewer (c/bt_viewer.c).

The Python viewer reads the index and the layout as npz and JSON, then
builds its glyph atlases with Pillow and fontTools. The C viewer does
neither: this step does it once and writes everything into one binary
the C side reads without a parser, native and in the browser alike:

    ./atlas_export.py data/war-and-peace_atlas          # -> data/war-and-peace_atlas/viewer.bin
    ./atlas_export.py data/war-and-peace_atlas --gzip   # and viewer.bin.gz, for the web page

The file is a table of named arrays. A 16 byte header ('BTAT', version,
count, 0), then `count` entries of 96 bytes (name[48], dtype[4] as
'u1' 'u2' 'u4' 'u8' 'i4' 'f4' 'f8', ndim, three u64 dims, u64 offset,
u64 bytes), then the data, each array at a 64 byte boundary. Everything
is little endian. It holds

  the index's and the layout's arrays, under their own names, but for a
    proportional face's char_x: the book's biggest array, a float per
    character that barely compresses, goes as char_dx (u2), each
    character's x less the one before it in its row, both rounded to
    1 / char_x_scale of a line height first so the running sum in the
    viewer lands on the rounded x exactly (a row's first character is 0)
  the scheme resolved over the code atlas's defaults: sc_ground, sc_page,
    sc_bar, sc_band (when the scheme names one), sc_kinds [10, 3],
    sc_items [5, 3], sc_ink (measured from the face when not named)
  the corpus face's raster atlas (glyphs [h, w] and glyph_m: cell_w,
    cell_h, char_aspect), its advances (adv [224], in line heights), the
    UI's monospace atlas (ui_glyphs, ui_m) and the vector tier (vt_curves,
    vt_bands) when the viewer draws with it
  scalars as f8 [1]: char_aspect, proportional, flat, median_pitch
  strings as a Windows-1252 blob and u4 offsets (s_<name>, s_<name>_off):
    paths, item_names, item_kws (the hover label's keyword per item),
    dir_labels, dir_tags (empty for a directory that draws none), name,
    tint (the tint's label, empty when the corpus has none)
  dir_parent (i4), the layout's or the index's parent of each directory
"""
import argparse
import gzip
import shutil
import struct
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_font  # noqa: E402
import atlas_viewer as av  # noqa: E402

HERE = Path(__file__).resolve().parent
MAGIC, VERSION = b"BTAT", 1
ALIGN = 64
DTYPES = {np.dtype(np.uint8): "u1", np.dtype(np.uint16): "u2", np.dtype(np.uint32): "u4",
          np.dtype(np.uint64): "u8", np.dtype(np.int32): "i4", np.dtype(np.float32): "f4",
          np.dtype(np.float64): "f8"}


def strings(items):
    """(blob, offsets) of the strings in Windows-1252, '?' for what it lacks."""
    enc = [s.encode(atlas_font.CHARSET, "replace") for s in items]
    off = np.zeros(len(enc) + 1, np.uint32)
    off[1:] = np.cumsum([len(b) for b in enc])
    return np.frombuffer(b"".join(enc) or b"\0", np.uint8), off


def dir_tags(a, labels, parent, children):
    """The tag each directory draws, as the Python viewer's build_dir_tags:
    a directory filled by one child draws none, and the innermost of such
    a chain draws the chain's names joined."""
    r, pad = a["dir_rect"], a["dir_pad"]
    area = (r[:, 2] - r[:, 0]) * (r[:, 3] - r[:, 1])
    inner = np.maximum(r[:, 2] - r[:, 0] - 2 * pad, 0) * np.maximum(r[:, 3] - r[:, 1] - 2 * pad, 0)
    tags = [""] * len(r)
    chain = [""] * len(r)
    for d in np.argsort(a["dir_depth"], kind="stable"):
        if d == 0:
            continue
        name = labels[d].rstrip("/")
        prefix = chain[parent[d]]
        kids = children[d]
        if len(kids) == 1 and area[kids[0]] >= 0.85 * inner[d]:
            chain[d] = f"{prefix} · {name}" if prefix else name
        else:
            tags[d] = f"{prefix} · {name}" if prefix else labels[d]
    return tags


def item_keywords(a, index):
    """The keyword the hover label names each item by: the item's own from
    its first line ('const', 'trait'), else the kind table's."""
    enc = index.get("encoding", "ascii")
    langs = [f.get("lang") for f in index["files"]]
    out = []
    for it in range(len(a["item_file"])):
        f = int(a["item_file"][it])
        first = int(a["file_line0"][f] + a["item_start"][it])
        lo, hi = int(a["line_off"][first]), int(a["line_off"][first + 1])
        m = av.ITEM_KW_RX.search(a["chars"][lo:hi].tobytes().decode(enc, "replace"))
        if m:
            out.append(m.group(1))
        else:
            names = av.ITEM_NAMES.get(langs[f], av.ITEM_NAMES[None])
            out.append(names[min(max(int(a["item_kind"][it]), 1), 5) - 1])
    return out


def char_deltas(a):
    """(char_dx u2, scale): char_x as rounded steps within each row, at
    the finest power-of-two scale up to 8192 whose largest step fits."""
    q = a["char_x"].astype(np.float64)
    row_start = np.zeros(len(q), bool)
    starts = np.concatenate(([0], np.cumsum(a["row_len"].astype(np.int64))[:-1]))
    row_start[starts[a["row_len"] > 0]] = True
    step = np.diff(q, prepend=0.0)
    step[row_start] = q[row_start]
    scale = 8192.0
    while scale > 1 and step.max() * scale >= 65535:
        scale /= 2
    qi = np.round(q * scale).astype(np.int64)
    dq = np.diff(qi, prepend=0)
    dq[row_start] = qi[row_start]
    if dq.min() < 0 or dq.max() > 65535:
        raise SystemExit("error: char_x does not rise along its rows; rerun ./atlas_layout.py")
    return dq.astype(np.uint16), scale


def median_pitch(a):
    """The pitch of the file holding the median line, the viewer's typical pitch."""
    order = np.argsort(a["file_pitch"], kind="stable")
    per_file = np.diff(a["file_line0"])[order]
    mid = min(int(np.searchsorted(np.cumsum(per_file), per_file.sum() / 2)), len(order) - 1)
    return float(a["file_pitch"][order[mid]])


def collect(atlas_dir, vector_text=True):
    """{name: array} of everything the C viewer reads."""
    a = av.load_atlas(atlas_dir)
    index, layout = a["index"], a["layout"]
    out = {k: v for k, v in a.items() if isinstance(v, np.ndarray)}
    if "char_x" in out:
        out["char_dx"], scale = char_deltas(a)
        out["char_x_scale"] = np.array([scale])
        del out["char_x"]

    sc = dict(av.SCHEME, **index.get("scheme", {}))
    for key in ("ground", "page", "bar"):
        out[f"sc_{key}"] = av.rgb(sc[key].lstrip("#"))
    if sc.get("band"):
        out["sc_band"] = av.rgb(sc["band"].lstrip("#"))
    out["sc_kinds"] = np.array([av.rgb(h.lstrip("#")) for h in sc["kinds"]], np.float32)
    out["sc_items"] = np.array([av.rgb(h.lstrip("#")) for h in sc["items"]], np.float32)
    leading = float(sc.get("leading", 1.0))
    font = str(HERE / sc["font"]) if sc.get("font") else atlas_font.DEFAULT_FONT

    glyphs, gm = atlas_font.build_atlas(font, 0, 64, leading)
    out["glyphs"] = glyphs
    out["glyph_m"] = np.array([gm["cell_w"], gm["cell_h"], gm["char_aspect"]], np.float32)
    adv = np.zeros(224, np.float32)
    adv[:len(gm["advances"])] = gm["advances"]
    out["adv"] = adv
    ui, um = atlas_font.build_atlas(atlas_font.DEFAULT_FONT, 0, 64)
    out["ui_glyphs"] = ui
    out["ui_m"] = np.array([um["cell_w"], um["cell_h"], um["char_aspect"]], np.float32)
    if sc.get("ink"):
        out["sc_ink"] = np.array(sc["ink"], np.float32)
    else:                                  # measured, as the Python viewer does
        freq = np.bincount(a["chars"], minlength=256)[32:256]
        ink, _ = atlas_font.mean_ink(font, 0, leading, freq)
        block = ink / av.BAR_HEIGHT
        out["sc_ink"] = np.array([av.INK_FAR, block * 1.25, block], np.float32)
    if vector_text:
        curves, bands, _ = av.load_vector_tier(font, 0, leading)
        out["vt_curves"] = np.ascontiguousarray(curves, np.float32)
        out["vt_bands"] = np.ascontiguousarray(bands, np.uint16)

    out["char_aspect"] = np.array([float(layout.get("char_aspect", 0.6))])
    out["proportional"] = np.array([1.0 if layout.get("proportional") else 0.0])
    out["flat"] = np.array([1.0 if layout.get("flat") else 0.0])
    out["median_pitch"] = np.array([median_pitch(a)])

    jd = layout["dirs"]
    labels = [d["label"] for d in jd]
    if jd and "parent" in jd[0]:
        parent, children = [d["parent"] for d in jd], [d["children"] for d in jd]
    else:
        parent = [d["parent"] for d in index["dirs"]]
        children = [d["children"] for d in index["dirs"]]
    out["dir_parent"] = np.array(parent, np.int32)
    tint = (index.get("tint") or "") if "file_tint" in a else ""
    for name, items in (("paths", [f["path"] for f in index["files"]]),
                        ("item_names", [it["name"] for it in index["items"]]),
                        ("item_kws", item_keywords(a, index)),
                        ("dir_labels", labels),
                        ("dir_tags", dir_tags(a, labels, parent, children)),
                        ("name", [index.get("name", Path(atlas_dir).name)]),
                        ("tint", [tint])):
        out[f"s_{name}"], out[f"s_{name}_off"] = strings(items)
    return out


def write(path, arrays):
    names = sorted(arrays)
    head = 16 + 96 * len(names)
    off = -(-head // ALIGN) * ALIGN
    entries, blobs = [], []
    for name in names:
        v = arrays[name]
        if v.dtype not in DTYPES:
            raise SystemExit(f"error: {name}: dtype {v.dtype} has no code")
        v = np.ascontiguousarray(v.astype(v.dtype.newbyteorder("<"), copy=False))
        dims = list(v.shape) + [1] * (3 - v.ndim)
        entries.append(struct.pack("<48s4sI3QQQ", name.encode(), DTYPES[v.dtype].encode(),
                                   v.ndim, *dims, off, v.nbytes)[:96].ljust(96, b"\0"))
        blobs.append((off, v.tobytes()))
        off = -(-(off + v.nbytes) // ALIGN) * ALIGN
    with open(path, "wb") as fh:
        fh.write(MAGIC + struct.pack("<III", VERSION, len(names), 0))
        for e in entries:
            fh.write(e)
        for o, b in blobs:
            fh.seek(o)
            fh.write(b)
        fh.truncate(off)
    return off


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("atlas", help="data/<name>_atlas directory, laid out")
    ap.add_argument("--out", default=None, help="output file (default <atlas>/viewer.bin)")
    ap.add_argument("--gzip", action="store_true", help="also write <out>.gz, for the web page")
    ap.add_argument("--no-vector-text", dest="vector_text", action="store_false",
                    help="leave the vector tier out: glyphs from the raster atlas at every size")
    args = ap.parse_args()
    av.check_atlas(args.atlas)
    t0 = time.perf_counter()
    out = Path(args.out or Path(args.atlas) / "viewer.bin")
    arrays = collect(args.atlas, args.vector_text)
    n = write(out, arrays)
    msg = f"wrote {out}: {len(arrays)} arrays, {n / 1e6:.1f} MB"
    if args.gzip:
        gz = out.with_name(out.name + ".gz")
        with open(out, "rb") as src, gzip.open(gz, "wb", compresslevel=9) as dst:
            shutil.copyfileobj(src, dst)
        msg += f"; {gz} {gz.stat().st_size / 1e6:.1f} MB"
    print(f"{msg}; {time.perf_counter() - t0:.1f} s")


if __name__ == "__main__":
    main()
