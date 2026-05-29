#!/bin/sh
set -e

PREFIX="${1:-default}"
DATE=$(date +%Y-%m-%d_%H-%M-%S)
RESULT_FILE="/data/${DATE}-${PREFIX}-speedresult.txt"
DB_FILE="/data/benchmark.db"

python /app/benchmark.py \
    --db      "$DB_FILE" \
    --prefix  "$PREFIX" \
    --output  "$RESULT_FILE"
