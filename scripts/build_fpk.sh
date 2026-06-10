#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${1:-$ROOT/dist}"
FN_PACK="${FN_PACK:-}"
FN_PACK_URL="${FN_PACK_URL:-https://static2.fnnas.com/fnpack/fnpack-1.0.4-linux-amd64}"

if [ -z "$FN_PACK" ]; then
    if command -v fnpack >/dev/null 2>&1; then
        FN_PACK="$(command -v fnpack)"
    else
        FN_PACK="$ROOT/.tools/fnpack"
    fi
fi

if [ ! -x "$FN_PACK" ]; then
    mkdir -p "$(dirname "$FN_PACK")"
    python3 - "$FN_PACK_URL" "$FN_PACK" <<'PY'
import sys
import urllib.request

url, output = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(url, timeout=60) as response:
    data = response.read()
with open(output, "wb") as handle:
    handle.write(data)
PY
    chmod +x "$FN_PACK"
fi

# fnpack 会打包源码目录内所有文件，构建前必须清理测试/编译产生的缓存。
find "$ROOT" \( -type d -name "__pycache__" -o -type f -name "*.pyc" \) -print | while IFS= read -r path; do
    if [ -d "$path" ]; then
        rm -rf "$path"
    else
        rm -f "$path"
    fi
done

mkdir -p "$OUT_DIR"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

(
    cd "$TMP_DIR"
    "$FN_PACK" build -d "$ROOT"
) >/tmp/fpkconverter_fnpack_build.log 2>&1 || {
    cat /tmp/fpkconverter_fnpack_build.log >&2
    exit 1
}

if [ -f "$TMP_DIR/fpkconverter.fpk" ]; then
    mv "$TMP_DIR/fpkconverter.fpk" "$OUT_DIR/fpkconverter.fpk"
else
    cat /tmp/fpkconverter_fnpack_build.log >&2
    echo "未找到 fnpack 输出文件 fpkconverter.fpk" >&2
    exit 1
fi

echo "$OUT_DIR/fpkconverter.fpk"
