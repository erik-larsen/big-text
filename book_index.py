#!/usr/bin/env python3
"""Stage 1 for a book: a Project Gutenberg text -> data/<name>_atlas/index.npz
+ index.json, the index atlas_index.py writes for a source tree, so the
layout stage and the viewer need nothing new: the book is a corpus with
parts for directories and pages for files.

The hierarchy is parts over chapters over pages. A heading line such as
`BOOK ONE: 1805`, `PART I` or `FIRST EPILOGUE: 1813 - 20` opens a part,
`CHAPTER I` a chapter inside it, and every --page-lines lines of a chapter
are one page, `p. 412`, numbered through the book; a page never starts with
a blank line and a chapter ends its last page early, as in print. The front
matter before the first heading (title, author, contents) is the first
part. Gutenberg's own header and footer, the `*** START` and `*** END`
lines and what lies outside them, are dropped. The parts are the
directories and the pages the files, a page's path naming its chapter;
index.json carries "corpus": "book" and "page_lines", and atlas_layout.py
lays a book out as a grid of pages in reading order, one band of whole page
rows per part, instead of a treemap.

Lines stay as Gutenberg wrapped them (about 72 columns) and are normalised
to ASCII like source: curly quotes straight, the em dash `--`, accents
stripped (Natásha to Natasha), since both glyph tiers cover ASCII (README,
open question 2). Words are the ident kind, numbers the number kind, the
rest punctuation; no string literals, no items.

  ./book_index.py --gutenberg 2600 --out data/war-and-peace_atlas
  ./book_index.py data/gutenberg/pg2600.txt --out data/war-and-peace_atlas
"""
import argparse
import os
import re
import sys
import time
import unicodedata
import urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atlas_index import (SPECS, MAX_COLS, TAB, PUNCT, SPACE, COMMENT, _CTRL, tokenize, write_index)  # noqa: E402
from atlas_layout import font_advances  # noqa: E402

# the book's colours, Dobbie's: white pages on his demo's blue-grey ground
# (its clearColor), black text, dark grey bars, the bands in the ground's
# own colour so parts draw nothing; words and numbers alike. The face is
# Literata, a book serif under the OFL, proportional: the layout and the
# viewer take its advances from the face
SCHEME = {"ground": "a0a9af", "page": "ffffff", "bar": "000000", "band": "a0a9af",
          "font": "fonts/Literata-Regular.ttf",
          # ink: the alpha of the bars far and near, and of the word blocks: a
          # page seen from far reads as the pale grey of averaged text
          "ink": [0.22, 0.22, 0.4],
          "kinds": ["000000", "141414", "141414", "141414", "141414", "6a6a6a", "141414", "4a4a4a", "141414", "141414"],
          "items": ["7aa2f7", "e0c080", "d7a0a8", "5fb7b7", "8c909a"]}

PART_RX = re.compile(r"^(?:(?:BOOK|PART|VOLUME)\s+[A-Z0-9]+\b|(?:FIRST|SECOND|THIRD)\s+EPILOGUE\b"
                     r"|EPILOGUE\b|PROLOGUE\b)(?P<rest>.*)$")
CHAPTER_RX = re.compile(r"^CHAPTER\s+[IVXLCDM0-9]+\.?\s*$")
START_RX = re.compile(r"^\*\*\* ?START OF")
END_RX = re.compile(r"^\*\*\* ?END OF")
GUTENBERG_URL = "https://www.gutenberg.org/cache/epub/{n}/pg{n}.txt"

# characters outside ASCII that have an ASCII spelling; accents are
# stripped by decomposition, everything else becomes '?'
ASCII_MAP = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'",
                           "—": "--", "–": "-", "…": "...", "•": "*",
                           "™": "(TM)", "œ": "oe", "Œ": "OE", "æ": "ae",
                           "Æ": "AE", "ß": "ss", " ": " "})


def header_field(lines, key):
    """`Title: War and Peace` from Gutenberg's header, or None."""
    rx = re.compile(r"^" + key + r":\s*(.+?)\s*$")
    for ln in lines[:200]:
        m = rx.match(ln)
        if m:
            return m.group(1)
    return None


class Measure:
    """Line widths in the book's face, in line heights, for the footer and
    the centred headings: the face's advances from atlas_layout.font_advances."""
    def __init__(self, face):
        adv, _ = font_advances(face)
        self.adv = [0.0] * 128
        self.adv[32:127] = adv["per_glyph"]
        self.space = self.adv[32]

    def width(self, text):
        return sum(self.adv[ord(c)] if ord(c) < 128 else self.adv[63] for c in to_ascii(text))

    def spaces(self, w, floor=False):
        n = int(w / self.space) if floor else int(round(w / self.space))
        return max(n, 1)


def to_ascii(text):
    text = text.translate(ASCII_MAP)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.encode("ascii", "replace").decode("ascii")


def title(heading):
    """`BOOK ONE: 1805` -> `Book One: 1805`; the numeral of `CHAPTER XII` stays."""
    words = heading.split()
    if CHAPTER_RX.match(heading):
        return words[0].capitalize() + " " + " ".join(words[1:])
    return " ".join(w.capitalize() for w in words)


def body_lines(text):
    """The lines between Gutenberg's START and END markers (all of them
    when the markers are missing)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    a = next((i + 1 for i, ln in enumerate(lines) if START_RX.match(ln)), 0)
    b = next((i for i, ln in enumerate(lines) if END_RX.match(ln)), len(lines))
    return lines[a:b]


def split_book(lines, front_label):
    """[(part heading, [(chapter heading, [lines]), ...]), ...] in reading
    order. Lines before the first chapter of a part go with the part's first
    chapter (the part's own heading), lines before the first part make the
    front matter, one chapter under front_label."""
    parts = []
    part, chapter = None, None

    def new_part(heading):
        nonlocal part, chapter
        part = (heading, [])
        parts.append(part)
        chapter = None

    def new_chapter(heading):
        nonlocal chapter
        if part is None:
            new_part(front_label)
        chapter = (heading, [])
        part[1].append(chapter)

    new_part(front_label)
    new_chapter("")
    for ln in lines:
        s = ln.rstrip()
        # a heading starts at column 0; the contents listing indents its copies
        if PART_RX.match(s):
            new_part(s)
            new_chapter("")
            chapter[1].append(s)
            continue
        if CHAPTER_RX.match(s):
            if chapter[0] == "" and all(not x.strip() or x.strip() == part[0] for x in chapter[1]):
                chapter = (s, chapter[1])       # the part heading's own lines lead into chapter one
                part[1][-1] = chapter
            else:
                new_chapter(s)
            chapter[1].append(s)
            continue
        chapter[1].append(ln)
    # drop empty chapters and parts
    out = []
    for heading, chapters in parts:
        chapters = [(h, ls) for h, ls in chapters if any(x.strip() for x in ls)]
        if chapters:
            out.append((heading, chapters))
    return out


def pages_of(lines, page_lines):
    """Chunks of at most page_lines lines, blank lines squeezed at each
    page's top and bottom, at most one blank line in a row."""
    kept, blank = [], 0
    for ln in lines:
        if ln.strip():
            kept.append(ln)
            blank = 0
        elif kept and blank == 0:
            kept.append("")
            blank = 1
    while kept and not kept[-1].strip():
        kept.pop()
    pages, i = [], 0
    while i < len(kept):
        while i < len(kept) and not kept[i].strip():
            i += 1
        page = kept[i:i + page_lines]
        while page and not page[-1].strip():
            page.pop()
        if page:
            pages.append(page)
        i += page_lines
    return pages


def index_page(relpath, lines, footer=False):
    """The ("ok", ...) tuple atlas_index.index_file returns, for prose. With
    `footer` the last line is the running footer, in the comment kind so
    the scheme can grey it."""
    raw = [to_ascii(ln).expandtabs(TAB).encode("ascii").translate(_CTRL).rstrip()[:MAX_COLS]
           for ln in lines]
    joined = b"\n".join(raw)
    line_len = np.fromiter((len(ln) for ln in raw), np.int64, len(raw))
    line_indent = np.fromiter((len(ln) - len(ln.lstrip(b" ")) for ln in raw), np.int64, len(raw))
    kinds = tokenize(joined, SPECS["prose"])
    cf = np.frombuffer(joined, np.uint8)
    kf = np.frombuffer(bytes(kinds), np.uint8).copy()
    kf[(cf == 32) & (kf == PUNCT)] = SPACE
    if footer and len(raw):
        tail = len(raw[-1])
        if tail:
            last = kf[-tail:]
            last[(cf[-tail:] != 32)] = COMMENT
    keep = cf != 10
    chars, kinds = cf[keep], kf[keep]
    nbytes = sum(len(ln.encode("utf-8")) + 1 for ln in lines)
    return ("ok", relpath, nbytes, "prose", chars.tobytes(), kinds.tobytes(),
            line_len.astype(np.uint16), line_indent.astype(np.uint16), int((chars != 32).sum()), [])


def page_width(parts, measure):
    """The book's line width in line heights: the 99.5th percentile of its
    lines' widths in the face, the width the footer spans and headings
    centre on."""
    widths = [measure.width(ln) for _, chapters in parts for _, lines in chapters for ln in lines if ln.strip()]
    return float(np.percentile(widths, 99.5)) if widths else 40.0


def footer_line(measure, width, author, number, title):
    """`Leo Tolstoy   653   War and Peace`: the author at the left, the
    number centred, the title flush right on a line `width` line heights
    wide, spaced with the face's own space."""
    w_a, w_n, w_t = measure.width(author), measure.width(number), measure.width(title)
    s1 = measure.spaces(width / 2 - w_a - w_n / 2)
    s2 = measure.spaces(width - w_a - s1 * measure.space - w_n - w_t, floor=True)   # never past the width
    return author + " " * s1 + number + " " * s2 + title


def build(parts, page_lines, name, measure=None, width=None, author="", book_title=""):
    """dirs, files, file_dir, results in atlas_index's pre-order: the root,
    then each part with its pages. Chapters are not directories, a page's
    path names its chapter (`Book One: 1805/Chapter III/p. 27`), so the
    layout is a grid of pages per part, as printed. A text without any
    heading is one part named after the corpus, its pages `name/p. N`.
    Every page is padded to page_lines lines and given a blank line and a
    running footer, and chapter headings are centred on the page width, as
    a printed page has them."""
    dirs = [{"path": "", "parent": -1, "children": [], "files": [], "top": 0}]
    files, file_dir, results = [], [], []
    page_no = 0
    plain = len(parts) == 1 and len(parts[0][1]) == 1 and parts[0][1][0][0] == ""
    for heading, chapters in parts:
        p = len(dirs)
        label = name if plain else title(heading)
        dirs.append({"path": label, "parent": 0, "children": [], "files": [],
                     "top": p, "label": label})
        dirs[0]["children"].append(p)
        for ch_heading, lines in chapters:
            chapter = "" if plain else "/" + (title(ch_heading) if ch_heading else "Contents")
            for page in pages_of(lines, page_lines):
                page_no += 1
                rel = f"{dirs[p]['path']}{chapter}/p. {page_no}"
                dirs[p]["files"].append(len(files))
                files.append(rel)
                file_dir.append(p)
                if measure is not None:
                    page = [(" " * measure.spaces((width - measure.width(ln)) / 2) + ln.strip())
                            if CHAPTER_RX.match(ln.rstrip()) else ln for ln in page]
                    page = page + [""] * (page_lines - len(page)) + ["", footer_line(measure, width, author, str(page_no), book_title)]
                results.append(index_page(rel, page, footer=measure is not None))
    return dirs, files, file_dir, results


def fetch(n, out_dir):
    path = os.path.join(out_dir, f"pg{n}.txt")
    if not os.path.exists(path):
        url = GUTENBERG_URL.format(n=n)
        print(f"fetching {url}")
        os.makedirs(out_dir, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "big-text book_index.py"})
        with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
            f.write(r.read())
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?", metavar="TXT", help="a Project Gutenberg plain text")
    ap.add_argument("--gutenberg", type=int, default=None, metavar="N",
                    help="fetch ebook N from gutenberg.org into data/gutenberg/ (once) instead")
    ap.add_argument("--out", default=None, help="output dir (default: data/<name>_atlas)")
    ap.add_argument("--name", default=None, help="corpus name (default: from --out, else the file name)")
    ap.add_argument("--page-lines", type=int, default=52,
                    help="lines per page (default 52: War and Peace makes 1401 pages, Dobbie's "
                         "reflowed pages numbered 1273)")
    ap.add_argument("--front", default="Front matter", help="label of the part before the first heading")
    args = ap.parse_args()
    if args.gutenberg is not None:
        args.text = fetch(args.gutenberg, os.path.join("data", "gutenberg"))
    if not args.text:
        sys.exit("give a text file or --gutenberg N")
    if args.name is None:
        if args.out:
            name = os.path.basename(os.path.normpath(args.out))
            args.name = name[:-6] if name.endswith("_atlas") else name
        else:
            args.name = os.path.splitext(os.path.basename(args.text))[0]
    if args.out is None:
        args.out = os.path.join("data", f"{args.name}_atlas")

    t0 = time.time()
    with open(args.text, "rb") as f:
        text = f.read().decode("utf-8", "replace")
    header = text.replace("\r\n", "\n").split("\n")
    book_title = header_field(header, "Title") or args.name
    author = header_field(header, "Author") or ""
    # Gutenberg writes honorifics lowercase before the name (`graf Leo Tolstoy`)
    author = " ".join(w for i, w in enumerate(author.split()) if not (w.islower() and i == 0))
    lines = body_lines(text)
    parts = split_book(lines, args.front)
    measure = Measure(SCHEME["font"])
    width = page_width(parts, measure)
    dirs, files, file_dir, results = build(parts, args.page_lines, args.name, measure, width, author, book_title)
    skipped = {"binary": 0, "large": 0, "submodules": 0}
    seconds = round(time.time() - t0, 2)
    stats = write_index(args.out, args.name, os.path.abspath(args.text), dirs, files, file_dir,
                        results, skipped, seconds,
                        extra={"corpus": "book", "page_lines": args.page_lines, "page_rows": args.page_lines + 2,
                               "title": book_title, "author": author, "scheme": SCHEME})
    n_chapters = sum(len(c) for _, c in parts)
    print(f"wrote {args.out}/index.npz + index.json: {len(parts)} parts, {n_chapters} chapters, "
          f"{stats['files']} pages, {stats['lines']} lines, {stats['chars'] / 1e6:.2f} M chars "
          f"({sum(r[8] for r in results) / 1e6:.2f} M glyphs), {seconds}s")


if __name__ == "__main__":
    main()
