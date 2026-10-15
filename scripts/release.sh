#!/usr/bin/env bash
# Publish a new version:  ./scripts/release.sh 0.3.1
# Updates manifest.json, commits, tags and pushes. GitHub Actions then runs the
# tests and, only if they pass, creates the release (HACS sees it as an update).
set -euo pipefail

VERSION="${1:-}"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Uso: $0 X.Y.Z   (es. $0 0.3.1)"; exit 1
fi

cd "$(git rev-parse --show-toplevel)"
MANIFEST=custom_components/icloud/manifest.json

if [ -n "$(git status --porcelain)" ]; then
  echo "Ci sono modifiche non salvate in git: fai prima commit (o stash)."; git status --short; exit 1
fi
if git rev-parse "v$VERSION" >/dev/null 2>&1; then
  echo "Il tag v$VERSION esiste già."; exit 1
fi

git pull --ff-only

python3 - "$MANIFEST" "$VERSION" <<'PY'
import json, sys
path, version = sys.argv[1], sys.argv[2]
data = json.load(open(path))
data["version"] = version
open(path, "w").write(json.dumps(data, indent=2) + "\n")
PY

if ! git diff --quiet "$MANIFEST"; then
  git add "$MANIFEST"
  git commit -m "Release v$VERSION"
fi

git tag -a "v$VERSION" -m "v$VERSION"
git push origin HEAD
git push origin "v$VERSION"

URL=$(git remote get-url origin | sed -E 's#git@github.com:#https://github.com/#; s#\.git$##')
echo
echo "Pubblicato v$VERSION. Segui la pipeline qui: $URL/actions"
