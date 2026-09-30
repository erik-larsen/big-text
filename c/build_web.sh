#!/bin/sh
# Build the C viewer for the browser (WebAssembly + WebGL 2).
# Usage: ./build_web.sh   (needs emcc on PATH: source <emsdk>/emsdk_env.sh)
# Output: ../web/bt_viewer.{js,wasm}, loaded by ../web/index.html, which
# fetches an atlas's viewer.bin.gz into the in-memory filesystem before main().
set -e
cd "$(dirname "$0")"

if ! command -v emcc >/dev/null 2>&1; then
    echo "emcc not found: install emsdk, then source <emsdk>/emsdk_env.sh" >&2
    exit 1
fi

# The shaders are the Python viewer's, embedded at /shaders.
emcc -O2 -Wall bt_viewer.c -o ../web/bt_viewer.js \
    -sUSE_SDL=2 -sMIN_WEBGL_VERSION=2 -sMAX_WEBGL_VERSION=2 \
    -sALLOW_MEMORY_GROWTH -sENVIRONMENT=web \
    -sEXPORTED_RUNTIME_METHODS=FS,addRunDependency,removeRunDependency \
    --embed-file ../shaders@/shaders

echo "built: ../web/bt_viewer.js + .wasm"
