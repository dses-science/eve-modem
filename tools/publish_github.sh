#!/usr/bin/env bash
# publish_github.sh - refresh the public GitHub mirror of this repository.
#
# The NAS bare repo (origin) stays the master. GitHub holds a read-only copy for the
# team and ORI, with the private working notes (CLAUDE.md) removed from EVERY commit:
# a fresh clone of the local repo is rewritten with git filter-repo, then force-pushed.
# The public history carries each commit's author only: co-author trailers are removed
# from every commit and tag message, and the push is refused if one survives.
# The rewrite is deterministic, so repeated runs produce the same history and the
# mirror's commit ids stay stable; tags are carried across.
#
#   bash tools/publish_github.sh              # mirror main + tags to dses-science/eve-modem
#   GITHUB_REPO=<owner>/<name> bash tools/publish_github.sh   # mirror to another repository
#
# Run it after pushing to origin (and as the last step of a release cut). Needs
# git filter-repo (pip install git-filter-repo) and a GitHub login (gh auth login,
# or a credential helper for https://github.com).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${GITHUB_REPO:-dses-science/eve-modem}"
URL="https://github.com/${REPO}.git"
# private working notes, local hooks, and third-party drawings not ours to republish
EXCLUDE=(CLAUDE.md .githooks docs/hardware/Ettus_B210_Enclosure_Drawing.pdf)
# filter-repo message callback (Python, bytes): drop co-author trailer lines
TRAILERS='import re
return re.sub(rb"(?im)^[ \t]*co-authored-by:[^\n]*(?:\n|$)", b"", message).rstrip() + b"\n"'

if [ -n "$(git -C "$here" status --porcelain)" ]; then
    echo "working tree not clean; commit or stash first" >&2
    exit 1
fi
if ! git -C "$here" filter-repo --version >/dev/null 2>&1; then
    echo "git filter-repo is not installed (pip install git-filter-repo)" >&2
    exit 1
fi
case "$(uname)" in
    MINGW*|MSYS*)
        # Git for Windows: use the Windows certificate store. A stale user gitconfig on
        # this machine points http.sslCAInfo at a Vivado bundle that no longer exists.
        export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.sslBackend GIT_CONFIG_VALUE_0=schannel ;;
esac
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
echo "== cloning the local repo into $work"
git clone -q --no-local "$here" "$work/mirror"
cd "$work/mirror"
args=()
for p in "${EXCLUDE[@]}"; do args+=(--path "$p"); done
echo "== removing ${EXCLUDE[*]} from every commit; co-author trailers from every message"
git filter-repo --quiet --invert-paths "${args[@]}" --message-callback "$TRAILERS"
if git log --all --name-only --format= -- "${EXCLUDE[@]}" | grep -q .; then
    echo "filter failed: excluded paths still present" >&2
    exit 1
fi
# Count, don't grep -q: under pipefail an early grep exit on a big log reads as "no match".
left="$( { git log --all --format=%B; git for-each-ref refs/tags --format='%(contents)'; } | grep -ci '^[[:space:]]*co-authored-by:' || true )"
if [ "$left" != "0" ]; then
    echo "filter failed: $left co-author trailer line(s) still present" >&2
    exit 1
fi
echo "== $(git rev-list --count main) commits, $(git tag | wc -l | tr -d ' ') tags after filtering"
git remote add github "$URL"
echo "== pushing to $URL"
git push -q --force github main
git push -q --force github --tags
echo "== done: https://github.com/${REPO}"
