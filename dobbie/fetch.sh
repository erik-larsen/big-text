#!/bin/sh
# Downloads Will Dobbie's two WebGL demos from wdobbie.com into this
# directory and verifies every file by MD5. Nothing of his is committed to
# this repository (no licence was ever published for the demos): git keeps
# this script and the README, and the pdf/ and warandpeace/ directories are
# ignored. `fetch.sh --check` only verifies what is present.
set -e
cd "$(dirname "$0")"
check_only=0
[ "$1" = "--check" ] && check_only=1
fetch() {
    path=$1; sum=$2
    if [ ! -f "$path" ]; then
        if [ $check_only = 1 ]; then echo "missing $path"; return; fi
        echo "fetching $path"
        mkdir -p "$(dirname "$path")"
        curl -sSL -o "$path" "https://wdobbie.com/$path"
    fi
    got=$(md5 -q "$path" 2>/dev/null || md5sum "$path" | cut -d' ' -f1)
    if [ "$got" != "$sum" ]; then
        echo "checksum mismatch for $path: $got" >&2; exit 1
    fi
    echo "ok $path"
}
fetch pdf/index.html             9109460ef4a90aa3fce67bb08b6e4ae2
fetch pdf/web.vert               7c7da6be7e3795b3e6303e403d6cbd71
fetch pdf/font.frag              82d00c49e53fc59d99e9ab18d730a865
fetch pdf/atlas.bmp              961e515f0ea83bf713f2667b7c1fa8a1
fetch pdf/pages.json             8acc99caa96d5f1f4b7286ca11f929f6
fetch pdf/vertices.bmp           b5c611b92e8d52799351f4331b974b9b
fetch warandpeace/index.html     ec7be2093ce4a9e4bf687340a07d8e48
fetch warandpeace/atlas.bmp      b304e18f300400a0168d24dfc409316c
fetch warandpeace/atlasverts.bmp 26aeda3a3c027836d4ed83da2d73ebb5
fetch warandpeace/glyphs.bmp     1387b2d27aba95d3ffd9f1fce49fa25e
fetch warandpeace/imageverts.bmp 993342c3683d8fe03993fd0d8f237e15
fetch warandpeace/pages.json     7def81687e71d65b7f0849814658c788
