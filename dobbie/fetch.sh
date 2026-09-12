#!/bin/sh
# Downloads the two large vertex buffers from wdobbie.com and verifies them.
set -e
cd "$(dirname "$0")"
fetch() {
    path=$1; sum=$2
    if [ ! -f "$path" ]; then
        echo "fetching $path"
        curl -sSL -o "$path" "https://wdobbie.com/$path"
    fi
    got=$(md5 -q "$path" 2>/dev/null || md5sum "$path" | cut -d' ' -f1)
    if [ "$got" != "$sum" ]; then
        echo "checksum mismatch for $path: $got" >&2; exit 1
    fi
    echo "ok $path"
}
fetch warandpeace/glyphs.bmp 1387b2d27aba95d3ffd9f1fce49fa25e
fetch pdf/vertices.bmp       b5c611b92e8d52799351f4331b974b9b
