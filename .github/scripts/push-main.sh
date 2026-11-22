#!/usr/bin/env bash
# Commit the listed paths and push them to main.
# The other workflow may have pushed while this job was running. Rebase onto
# origin/main and retry. On a conflict, keep this job's copy of the paths it
# was asked to commit, and keep origin/main for every other path.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: push-main.sh <commit message> <file> [file...]" >&2
  exit 2
fi

msg=$1
shift
files=("$@")

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

for f in "${files[@]}"; do
  if [ -e "$f" ]; then
    git add -- "$f" || true
  else
    echo "skip missing $f"
  fi
done

if git diff --cached --quiet; then
  echo "No file changes to commit"
  exit 0
fi

git commit -m "$msg"

owns_file() {
  local f=$1 candidate
  for candidate in "${files[@]}"; do
    if [ "$candidate" = "$f" ]; then
      return 0
    fi
  done
  return 1
}

resolve_conflicts() {
  local f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if owns_file "$f"; then
      echo "conflict in $f; keeping this job's version"
      git checkout --theirs -- "$f" 2>/dev/null || git checkout --ours -- "$f"
    else
      echo "conflict in $f; keeping the version already on main"
      git checkout --ours -- "$f" 2>/dev/null || git checkout --theirs -- "$f"
    fi
    git add -- "$f"
  done < <(git diff --name-only --diff-filter=U)
}

rebase_onto_main() {
  if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    git rebase --abort
  fi
  if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
    git fetch --unshallow origin || git fetch --deepen=100 origin main
  fi
  git fetch origin main
  if git rebase origin/main; then
    return 0
  fi
  echo "Rebase stopped; resolving conflicts"
  local guard=0
  while [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; do
    guard=$((guard + 1))
    if [ "$guard" -gt 30 ]; then
      echo "rebase did not finish" >&2
      git rebase --abort || true
      return 1
    fi
    if [ -n "$(git diff --name-only --diff-filter=U)" ]; then
      resolve_conflicts
    fi
    if [ -z "$(git diff --name-only --diff-filter=U)" ] && git diff --cached --quiet; then
      GIT_EDITOR=true git rebase --skip
    else
      GIT_EDITOR=true git rebase --continue
    fi
  done
}

# Do not `return 1` from a function while errexit is on: bash exits the whole
# script instead of returning to the caller.
attempt=1
while [ "$attempt" -le 5 ]; do
  set +e
  push_out=$(git push origin HEAD:main 2>&1)
  push_code=$?
  set -e
  printf '%s\n' "$push_out"
  if [ "$push_code" -eq 0 ]; then
    echo "pushed to main on attempt $attempt"
    exit 0
  fi
  if ! printf '%s\n' "$push_out" | grep -E 'rejected|fetch first|non-fast-forward|cannot lock ref|failed to push some refs' >/dev/null; then
    echo "push failed for a reason a rebase will not fix" >&2
    exit 1
  fi
  echo "push rejected on attempt $attempt; rebasing onto origin/main"
  rebase_onto_main
  sleep "${PUSH_RETRY_SLEEP:-$attempt}"
  attempt=$((attempt + 1))
done

echo "git push failed after 5 attempts" >&2
exit 1
