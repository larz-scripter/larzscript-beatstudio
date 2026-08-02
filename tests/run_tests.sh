#!/bin/sh
# Runs each tests/*.sh scenario against a fresh temp project dir and diffs
# its combined output against tests/<name>.expected. Same discipline as
# every other larzscript-* showcase app's tests/run_tests.sh. Nothing here
# is random despite the drum synthesis using noise - the `random` package's
# PRNG starts from the same fixed default seed every fresh process (no
# script here ever calls seed()), so a given pattern renders byte-for-byte
# identically on every run - verified by running the full pipeline twice
# in independent temp dirs before writing these golden files.
#
# Env override: BEATSTUDIO="larzscript /path/to/beatstudio.lz" to test an
# installed copy instead of the repo one (used by CI).
set -e
cd "$(dirname "$0")/.."
BEATSTUDIO="${BEATSTUDIO:-larzscript beatstudio.lz}"
export BEATSTUDIO

pass=0; fail=0
for t in tests/*.sh; do
  case "$t" in *run_tests.sh) continue ;; esac
  exp="${t%.sh}.expected"
  tmpdir="$(mktemp -d)"
  tmp="$tmpdir/project.json"
  got="$(sh "$t" "$tmp" 2>&1 | sed "s#$tmpdir#PROJECT_DIR#g" || true)"
  rm -rf "$tmpdir"
  if [ "$got" = "$(cat "$exp")" ]; then
    pass=$((pass+1))
  else
    fail=$((fail+1)); echo "FAIL $t"; echo "--- expected ---"; cat "$exp"; echo "--- got ---"; echo "$got"
  fi
done
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
