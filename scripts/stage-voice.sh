#!/bin/sh
# `lk agent create` uploads one folder and builds it there, but the voice agent needs the shared
# package next to it. This copies both into a fresh folder, laid out as services/ is, with the
# Dockerfile at its root. Then:  cd <folder> && lk agent create .
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:?usage: stage-voice.sh <new-or-empty-folder>}
mkdir -p "$out"
[ -z "$(ls -A "$out")" ] || { echo "$out is not empty" >&2; exit 1; }

for service in common voice; do
  mkdir "$out/$service"
  cp -R "$root/services/$service/src" "$out/$service/src"
  cp "$root/services/$service/pyproject.toml" "$root/services/$service/uv.lock" "$out/$service/"
done
cp "$root/services/voice/.python-version" "$out/voice/"
cp "$root/services/voice/Dockerfile" "$out/Dockerfile"
cp "$root/services/.dockerignore" "$out/.dockerignore"
# lk checks the root for a Python project that names livekit-agents; the build itself uses the
# Dockerfile, which installs from voice/uv.lock.
echo "livekit-agents" > "$out/requirements.txt"
echo "staged in $out"
