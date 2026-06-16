#!/usr/bin/env bash
set -euo pipefail

if [[ -f .env ]]; then
    BASE_URL=$(grep -E '^BASE_URL=' .env | head -1 | cut -d= -f2- | tr -d '"')
fi

: "${BASE_URL:?BASE_URL must be set (define it in .env)}"
: "${UUID:?UUID must be set}"

mkdir -p "files/${UUID}"

echo "Fetching index: ${BASE_URL}/${UUID}/index.html"
curl -fsSL "${BASE_URL}/${UUID}/index.html" -o "files/${UUID}/index.html"

grep -oP '(?<=href=")[^"]+' "files/${UUID}/index.html" | while read -r file; do
    echo "Downloading: ${file}"
    mkdir -p "files/${UUID}/$(dirname "${file}")"
    curl -fsSL "${BASE_URL}/${UUID}/${file}" -o "files/${UUID}/${file}"
done

echo "Done. Files saved to files/${UUID}/"
