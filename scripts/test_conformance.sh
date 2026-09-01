#!/usr/bin/env bash
# Regenerate every corpus and fail if any committed file differs.
#
# There are three now, and naming only the first one — which is what this
# repository's test command did until the second and third were added — reports a
# check it is not running. Each generator is a separate implementation with a
# separate corpus, so each needs its own --check.
set -euo pipefail

cd "$(dirname "$0")/../conformance"

python3 generate.py --check
python3 generate_queries.py --check
python3 generate_json.py --check

echo "conformance: 3 corpora, all tests passed."
