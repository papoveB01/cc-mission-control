#!/usr/bin/env bash
# Local equivalent of .github/workflows/python.yml and ui-bundle.yml.
# Run before releases or while GitHub Actions is unavailable. See -h.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

QUICK=0
NO_UI=0

usage() {
  cat <<'USAGE'
Usage: scripts/ci-local.sh [--quick] [--no-ui] [-h|--help]

Runs the checks from .github/workflows/python.yml and ui-bundle.yml locally:
  1. Manifests         inline validation script, extracted from python.yml
  2. pytest            Python 3.10, 3.12 and the default interpreter
  3. Slow launcher     CCMC_SLOW_TESTS=1 tests/test_launcher.py
  4. UI                Node 22: npm ci, typecheck, test, build (in ui/)
  5. Bundle drift      cc_mission_control/static must match the fresh build

Options:
  --quick     default interpreter only; skip the 3.10/3.12 matrix and slow test
  --no-ui     skip the UI and bundle drift steps
  -h, --help  show this help

Per-version virtualenvs live in .ci-venvs/ (gitignored) so reruns are fast.
Exit status is non-zero if any step failed.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --quick) QUICK=1 ;;
    --no-ui) NO_UI=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required. Install it: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 2
fi

NAMES=()
RESULTS=()
TIMES=()
FAILED=0

record() { # name result seconds
  NAMES+=("$1"); RESULTS+=("$2"); TIMES+=("$3")
  [ "$2" = FAIL ] && FAILED=1
  return 0
}

# run_step "name" command [args...]  -> records PASS/FAIL; returns its status
run_step() {
  local name="$1" start rc=0
  shift
  echo
  echo "==> $name"
  start=$SECONDS
  "$@" || rc=$?
  if [ "$rc" -eq 0 ]; then record "$name" PASS $((SECONDS - start)); else record "$name" FAIL $((SECONDS - start)); fi
  return "$rc"
}

skip_step() { # name reason
  echo
  echo "==> $1"
  echo "SKIP: $2"
  NAMES+=("$1"); RESULTS+=("SKIP"); TIMES+=("-")
}

# ---- 1. manifests -----------------------------------------------------------
manifests() {
  local script
  script="$(mktemp -t ccmc-manifests.XXXXXX)" || return 1
  # Extract the heredoc body from the workflow and strip the YAML indentation.
  awk '
    !inb && /python - <<.PY./ { match($0, /^ */); indent = RLENGTH; inb = 1; next }
    inb { line = $0; sub(/^[ \t]+/, "", line); if (line == "PY") exit; print substr($0, indent + 1) }
  ' .github/workflows/python.yml >"$script"
  if [ ! -s "$script" ]; then
    echo "could not extract the validation script from .github/workflows/python.yml" >&2
    rm -f "$script"; return 1
  fi
  local rc=0
  uv run --quiet python "$script" || rc=$?
  rm -f "$script"
  return "$rc"
}

# ---- 2. pytest per interpreter ---------------------------------------------
pytest_on() { # [python version]; empty = default interpreter
  local ver="${1:-}" venv="$ROOT/.ci-venvs/py${1:-default}"
  mkdir -p "$ROOT/.ci-venvs"
  if [ -n "$ver" ]; then
    UV_PROJECT_ENVIRONMENT="$venv" uv run --quiet -p "$ver" --extra dev pytest -q
  else
    UV_PROJECT_ENVIRONMENT="$venv" uv run --quiet --extra dev pytest -q
  fi
}

py_available() {
  uv python find "$1" >/dev/null 2>&1 || uv python install "$1" >/dev/null 2>&1
}

# ---- 3. slow launcher -------------------------------------------------------
slow_launcher() {
  CCMC_SLOW_TESTS=1 uv run --quiet --extra dev pytest -q tests/test_launcher.py
}

# ---- 4. UI ------------------------------------------------------------------
ensure_node22() {
  local want
  want="$(tr -d '[:space:]' <ui/.nvmrc)"; want="${want#v}"; want="${want%%.*}"
  if [ "$(node --version 2>/dev/null | sed 's/^v//; s/\..*//')" = "$want" ]; then return 0; fi
  if [ -x "/opt/homebrew/opt/node@$want/bin/node" ]; then
    export PATH="/opt/homebrew/opt/node@$want/bin:$PATH"
  elif [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
    # shellcheck disable=SC1091
    (. "${NVM_DIR:-$HOME/.nvm}/nvm.sh" >/dev/null 2>&1 && nvm which "$want" >/dev/null 2>&1) \
      && { . "${NVM_DIR:-$HOME/.nvm}/nvm.sh" >/dev/null 2>&1; nvm use "$want" >/dev/null 2>&1 || true; }
  fi
  if [ "$(node --version 2>/dev/null | sed 's/^v//; s/\..*//')" != "$want" ]; then
    echo "Node $want is required (ui/.nvmrc) but found: $(node --version 2>/dev/null || echo none)." >&2
    echo "Install it: brew install node@$want, or nvm install $want && nvm use $want" >&2
    return 1
  fi
}

ui_checks() {
  ensure_node22 || return 1
  echo "node $(node --version)"
  (
    cd ui
    echo "--> npm ci";            npm ci || exit 1
    echo "--> npm run typecheck"; npm run typecheck || exit 1
    echo "--> npm test";          npm test || exit 1
    echo "--> npm run build";     npm run build || exit 1
  )
}

# ---- 5. bundle drift --------------------------------------------------------
bundle_drift() {
  local status
  status="$(git status --porcelain -- cc_mission_control/static)"
  if [ -n "$status" ]; then
    echo "$status"
    git diff --stat -- cc_mission_control/static || true
    echo "Run \`npm run build\` in ui/ and commit the bundle" >&2
    return 1
  fi
  echo "Committed bundle matches the fresh build."
}

# ---- run --------------------------------------------------------------------
TOTAL_START=$SECONDS

run_step "Manifests" manifests || true

if [ "$QUICK" -eq 0 ]; then
  for v in 3.10 3.12; do
    if py_available "$v"; then
      run_step "pytest py$v" pytest_on "$v" || true
    else
      echo; echo "==> pytest py$v"
      echo "WARNING: uv cannot provide Python $v; skipping" >&2
      NAMES+=("pytest py$v"); RESULTS+=("SKIP"); TIMES+=("-")
    fi
  done
fi
run_step "pytest default" pytest_on "" || true

if [ "$QUICK" -eq 0 ]; then
  run_step "Slow launcher test" slow_launcher || true
fi

if [ "$NO_UI" -eq 1 ]; then
  skip_step "UI (install, typecheck, test, build)" "--no-ui"
  skip_step "Bundle drift" "--no-ui"
elif run_step "UI (install, typecheck, test, build)" ui_checks; then
  run_step "Bundle drift" bundle_drift || true
else
  skip_step "Bundle drift" "UI build did not complete"
fi

echo
echo "==> Summary"
printf '%-40s %-6s %s\n' STEP RESULT TIME
printf '%-40s %-6s %s\n' ---------------------------------------- ------ ------
for i in "${!NAMES[@]}"; do
  t="${TIMES[$i]}"; [ "$t" = "-" ] || t="${t}s"
  printf '%-40s %-6s %s\n' "${NAMES[$i]}" "${RESULTS[$i]}" "$t"
done
echo "Total: $((SECONDS - TOTAL_START))s"
if [ "$FAILED" -ne 0 ]; then echo "FAIL"; exit 1; fi
echo "PASS"
