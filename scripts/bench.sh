#!/usr/bin/env sh
# Reproducible benchmark: every suite at full size with fixed seeds, then the
# predictor evaluation. Results land in results/bench-<server version>-<date>/.
set -eu
cd "$(dirname "$0")/.."

ROWS="${ROWS:-10000}"
VERSION="$(uv run python -c "from pagereuse import db; c = db.connect(); print(c.execute('SHOW server_version').fetchone()[0].split()[0]); c.close()")"
OUT="results/bench-pg${VERSION}-$(date -u +%Y%m%d)"
mkdir -p "$OUT"

for suite in fillfactor updates cycles snapshots concurrency overlap autovacuum; do
  uv run python -m pagereuse suite "$suite" --rows "$ROWS" --out "$OUT"
done
uv run python -m pagereuse evaluate --results "$OUT" | tee "$OUT/model_eval.txt"
echo "benchmark written to $OUT"
