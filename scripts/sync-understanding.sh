#!/bin/sh
# Copy the organism's definition into this site, and report whether it had drifted.
#
# `UNDERSTANDING.md` is authored in the tuscany-digital-twin repository. This site runs on a
# different host and cannot mount it, so it ships a COPY — and a copy can go stale silently,
# which for a document that defines what the organism IS would be worse than not showing it.
#
# So: run this after changing the source, and the check below fails loudly in CI or by hand if
# the copy is behind. The hash is printed so a drift is visible rather than inferred.
#
#   ./scripts/sync-understanding.sh          # copy and report
#   ./scripts/sync-understanding.sh --check  # report only, non-zero if stale
set -eu
cd "$(dirname "$0")/.."

SRC="${TDT_REPO:-$HOME/dev/tuscany-digital-twin}/UNDERSTANDING.md"
DST=public/docs/UNDERSTANDING.md

if [ ! -f "$SRC" ]; then
  echo "source not found: $SRC"
  echo "set TDT_REPO if the tuscany-digital-twin checkout lives elsewhere."
  exit 1
fi

src_hash=$(shasum -a 256 "$SRC" | cut -d' ' -f1)
dst_hash=$([ -f "$DST" ] && shasum -a 256 "$DST" | cut -d' ' -f1 || echo "absent")

if [ "$src_hash" = "$dst_hash" ]; then
  echo "in sync: ${src_hash%${src_hash#????????????????}}"
  exit 0
fi

echo "DRIFT:"
echo "  source $src_hash"
echo "  copy   $dst_hash"

if [ "${1:-}" = "--check" ]; then
  echo "run without --check to update the copy."
  exit 1
fi

mkdir -p public/docs
cp "$SRC" "$DST"
echo "updated $DST"
