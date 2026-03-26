#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt

# TARGET_REPO: "org/repo" — set in Render env vars (e.g. "acme/shield", "external-org/other-repo")
if [ -z "$TARGET_REPO" ]; then
  echo "ERROR: TARGET_REPO env var not set (expected 'org/repo')"
  exit 1
fi

# Clone target repo if not already present
REPO_DIR="/opt/render/project/src/target-repo"
if [ -d "$REPO_DIR/.git" ]; then
  cd "$REPO_DIR" && git pull origin main
else
  git clone "https://${GITHUB_PAT}@github.com/${TARGET_REPO}.git" "$REPO_DIR"
fi
