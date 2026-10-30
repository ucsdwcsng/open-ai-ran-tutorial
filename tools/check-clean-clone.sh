#!/usr/bin/env bash
# check-clean-clone.sh
#
# Catches "works in my tree, broken on a fresh clone" defects, statically.
# No build, no cluster, no submodule init required. Safe to run in CI.
#
# Every check here corresponds to a real breakage documented in BUILD-ISSUES.md.
#
#   exit 0 = clean, exit 1 = at least one blocker

set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "not a git repo"; exit 2; }

FAIL=0
pass() { printf '  \033[32mok\033[0m    %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=1; }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

CODELETS=jrtc-apps/codelets
APPS=jrtc-apps/jrtc_apps

# Which git repo owns a path? A .gitignore only governs its own repo, so a rule
# in the superproject cannot affect files inside a submodule -- that distinction
# is the whole reason converting jbpf_protobuf to a submodule fixed it there.
owner() { git -C "$1" rev-parse --show-toplevel 2>/dev/null; }

# ---------------------------------------------------------------- 1
# A .gitignore rule that shadows vendored *source*.
#
# Unanchored patterns match at every level. A bare `build` intended for a
# top-level CMake output dir also erases 3p/<lib>/build/, which for
# Concurrency Kit holds its configure templates -- see BUILD-ISSUES.md #1.
head_ "[1] .gitignore rules shadowing vendored source directories"
found_shadow=0
while IFS= read -r gi; do
    grep -qxF 'build' "$gi" 2>/dev/null || continue
    d=$(dirname "$gi")
    gi_repo=$(owner "$d")
    while IFS= read -r bd; do
        # Only a problem if that build/ holds template/source files, not outputs...
        compgen -G "$bd/*.in" >/dev/null 2>&1 || continue
        # ...and only if the rule and the files live in the same repo.
        [ "$(owner "$bd")" = "$gi_repo" ] || continue
        fail "$gi has an unanchored 'build' rule that excludes $bd (source). Use '/build'."
        found_shadow=1
    done < <(find "$d" -type d -name build -not -path '*/.git/*' 2>/dev/null)
done < <(find . -name .gitignore -not -path './.git/*' 2>/dev/null)
[ "$found_shadow" -eq 0 ] && pass "no unanchored 'build' rule hides vendored templates"

# ---------------------------------------------------------------- 2
# Concurrency Kit specifically: templates must be present AND tracked.
head_ "[2] Concurrency Kit build templates are tracked"
ck_any=0
while IFS= read -r ckdir; do
    ck_any=1
    # Count within the repo that actually owns this ck tree (it may be a submodule).
    n=$(git -C "$ckdir" ls-files build 2>/dev/null | wc -l | tr -d ' ')
    if [ "$n" -lt 15 ]; then
        fail "$ckdir/build has $n tracked files, expected 15 (configure will emit an empty ck.build)"
    else
        pass "$ckdir/build: $n templates tracked"
    fi
done < <(find . -type d -path '*/3p/ck' -not -path '*/.git/*' 2>/dev/null)
[ "$ck_any" -eq 0 ] && pass "no vendored ck tree in this repo"

# ---------------------------------------------------------------- 3
# Any codelet dir that ships a codeletset yaml must be buildable.
head_ "[3] every codelet directory with a yaml has a Makefile"
mk_bad=0
for d in "$CODELETS"/*/; do
    [ -d "$d" ] || continue
    compgen -G "$d*.yaml" >/dev/null 2>&1 || continue
    if [ ! -f "$d/Makefile" ]; then
        fail "${d%/} ships codeletset yaml(s) but no Makefile"
        mk_bad=1
    fi
done
[ "$mk_bad" -eq 0 ] && pass "all codelet dirs with yamls have Makefiles"

# ---------------------------------------------------------------- 4
# Every .o a codeletset references must be producible.
# Either <name>.cpp/.c exists, or the Makefile names <name>.o explicitly
# (e.g. several sized objects compiled from one source via -D flags).
head_ "[4] every referenced codelet object can be produced"
obj_bad=0
while IFS= read -r ref; do
    dir=${ref%/*}; obj=${ref##*/}; base=${obj%.o}
    src="$CODELETS/$dir"
    [ -d "$src" ] || { fail "codeletset references missing dir: $dir"; obj_bad=1; continue; }
    if [ -f "$src/$base.cpp" ] || [ -f "$src/$base.c" ]; then continue; fi
    if [ -f "$src/Makefile" ] && grep -q "$obj" "$src/Makefile"; then continue; fi
    fail "$dir/$obj has no $base.cpp/.c and no explicit Makefile rule"
    obj_bad=1
done < <(grep -rhoE '\$\{JBPF_CODELETS\}/[A-Za-z0-9_]+/[A-Za-z0-9_]+\.o' "$CODELETS" 2>/dev/null \
         | sed 's#${JBPF_CODELETS}/##' | sort -u)
[ "$obj_bad" -eq 0 ] && pass "all referenced .o files are producible"

# ---------------------------------------------------------------- 5
# Every python module an xApp imports must come from a dir the README builds.
# Modules are generated from the codelet sources, so an unbuilt dir means the
# app dies at import -- and jrtc-ctl still reports "loaded app". See #4/#5.
head_ "[5] xApp module imports are covered by the README build list"
# Join backslash continuations first -- the build list spans two lines.
BUILD_LINE=$(sed -e ':a' -e '/\\$/{N;s/\\\n//;ba}' README.md 2>/dev/null \
             | grep -m1 -oE 'for d in [a-z0-9_ ]+; do \./make\.sh' \
             | sed -E 's/^for d in //; s/; do \.\/make\.sh$//' | tr -s ' ')
if [ -z "$BUILD_LINE" ]; then
    fail "could not locate the codelet build list in README.md (expected a 'for d in ...' line)"
else
    mod_bad=0
    while IFS= read -r dir; do
        [ -d "$CODELETS/$dir" ] || { fail "xApp imports from missing codelet dir: $dir"; mod_bad=1; continue; }
        case " $BUILD_LINE " in
            *" $dir "*) ;;
            *) fail "xApp imports $dir/* but README build list omits '$dir'"; mod_bad=1 ;;
        esac
    done < <(grep -rhoE '\$\{JBPF_CODELETS\}/[A-Za-z0-9_]+/[A-Za-z0-9_]+\.py' "$APPS" 2>/dev/null \
             | sed -E 's#\$\{JBPF_CODELETS\}/([^/]+)/.*#\1#' | sort -u)
    [ "$mod_bad" -eq 0 ] && pass "every imported module's dir is in the build list"
fi

# ----------------------------------------------------------------
printf '\n'
if [ "$FAIL" -eq 0 ]; then
    printf '\033[32mclean-clone check passed\033[0m\n'
else
    printf '\033[31mclean-clone check FAILED\033[0m — see BUILD-ISSUES.md\n'
fi
exit "$FAIL"
