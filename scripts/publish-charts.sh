#!/usr/bin/env bash
# Package charts/* onto the aif/charts branch, the git ClusterRepo that Rancher
# Apps and SUSE AI Factory read. Rancher's git catalog indexes only a root
# index.yaml plus packaged .tgz files on the tracked branch.
#
#   scripts/publish-charts.sh          # package + commit on aif/charts
#   scripts/publish-charts.sh --push   # ... and push it
#
# Then register the repo once per Rancher management cluster:
#   kubectl apply -f catalog/clusterrepo.yaml
set -euo pipefail

BRANCH=aif/charts
URL=https://github.com/wiredquill/can-i
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PUSH=${1:-}

WORK=$(mktemp -d)
trap 'git -C "$ROOT" worktree remove --force "$WORK/branch" 2>/dev/null || true; rm -rf "$WORK"' EXIT

for c in "$ROOT"/charts/*/; do
  helm package "$c" -d "$WORK/pkg" >/dev/null
done
ls "$WORK/pkg"

git -C "$ROOT" fetch -q origin "$BRANCH:refs/remotes/origin/$BRANCH" 2>/dev/null || true
if git -C "$ROOT" rev-parse -q --verify "refs/heads/$BRANCH" >/dev/null; then
  git -C "$ROOT" worktree add -q "$WORK/branch" "$BRANCH"
elif git -C "$ROOT" rev-parse -q --verify "refs/remotes/origin/$BRANCH" >/dev/null; then
  git -C "$ROOT" worktree add -q -b "$BRANCH" "$WORK/branch" "origin/$BRANCH"
else
  # first publish: an orphan branch holding only the chart repository
  git -C "$ROOT" worktree add -q --detach "$WORK/branch"
  git -C "$WORK/branch" checkout -q --orphan "$BRANCH"
  git -C "$WORK/branch" rm -rqf .
fi

cd "$WORK/branch"
# chart versions are immutable once published: bump the version to ship changes
for t in "$WORK"/pkg/*.tgz; do
  name=$(basename "$t")
  if [ -e "$name" ]; then
    echo "skip $name: already published (bump the chart version to publish changes)"
  else
    cp "$t" .
  fi
done
if [ -z "$(git status --porcelain -- '*.tgz')" ]; then
  echo "nothing new to publish"
  exit 0
fi
helm repo index . --url "$URL"
git add index.yaml ./*.tgz
git commit -q -m "Publish charts: $(ls ./*.tgz | xargs -n1 basename | tr '\n' ' ')"
git log --oneline -1
if [ "$PUSH" = --push ]; then
  git push -q origin "$BRANCH"
  echo "pushed $BRANCH; refresh the ClusterRepo in Rancher to pick it up"
fi
