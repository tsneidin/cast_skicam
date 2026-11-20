#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
target_dir="${1:-/mnt/user/appdata/ski-cams}"

if [[ ! -f "$target_dir/compose.yaml" || ! -f "$target_dir/app.py" ]]; then
  echo "Existing ski camera Compose installation not found at $target_dir" >&2
  exit 1
fi
if [[ ! -f "$source_dir/catalog.py" || ! -f "$source_dir/cameras.json" ]]; then
  echo "Updated source files are missing from $source_dir" >&2
  exit 1
fi

(cd "$target_dir" && docker compose config --quiet)
backup_dir="$target_dir/previous-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
cp "$target_dir/app.py" "$target_dir/Dockerfile" "$target_dir/cameras.json" "$backup_dir/"
cp "$source_dir/app.py" "$source_dir/catalog.py" "$source_dir/cameras.json" "$source_dir/Dockerfile" "$source_dir/README.md" "$target_dir/"
(cd "$target_dir" && docker compose up -d --build)
(cd "$target_dir" && docker compose ps)
echo "Previous source: $backup_dir"
