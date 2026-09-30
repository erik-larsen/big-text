#!/bin/sh
# Build the C viewer against SDL2 (brew) and ANGLE GLES 3 (opengl-for-mac).
# Usage: OGL_FOR_MAC=/path/to/opengl-for-mac ./build_mac.sh
# Then:  ./bt_viewer ../data/war-and-peace_atlas   (after ./atlas_export.py)
set -e
cd "$(dirname "$0")"

OGL_FOR_MAC="${OGL_FOR_MAC:-$HOME/Github/opengl-for-mac}"
if [ ! -f "$OGL_FOR_MAC/include/GLES3/gl3.h" ]; then
    echo "no GLES3 headers under $OGL_FOR_MAC: clone erik-larsen/opengl-for-mac and set OGL_FOR_MAC" >&2
    exit 1
fi

clang -O2 -Wall -Wextra -Wno-unused-parameter $(sdl2-config --cflags) -I"$OGL_FOR_MAC/include" \
    -DBT_ANGLE_LIB_DIR="\"$OGL_FOR_MAC/lib\"" bt_viewer.c \
    $(sdl2-config --libs) -L"$OGL_FOR_MAC/lib" -lGLESv2 -lEGL -lm -Wl,-headerpad_max_install_names \
    -o bt_viewer

# The ANGLE dylibs carry cwd-relative install names (./libGLESv2.dylib);
# rewrite them to absolute paths so the viewer runs from anywhere without
# DYLD_FALLBACK_LIBRARY_PATH, then re-sign (required on arm64).
install_name_tool \
    -change ./libGLESv2.dylib "$OGL_FOR_MAC/lib/libGLESv2.dylib" \
    -change ./libEGL.dylib "$OGL_FOR_MAC/lib/libEGL.dylib" \
    bt_viewer
codesign -f -s - bt_viewer 2>/dev/null
echo "built: c/bt_viewer"
