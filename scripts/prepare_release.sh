#!/bin/bash
set -euo pipefail

REPOSITORY_DIRECTORY="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
cd "$REPOSITORY_DIRECTORY"

if command -v poetry >/dev/null 2>&1; then
  POETRY_EXECUTABLE="$(command -v poetry)"
elif [ -x ".venv/bin/poetry" ]; then
  POETRY_EXECUTABLE=".venv/bin/poetry"
else
  echo "Poetry is required to prepare a release." >&2
  exit 1
fi

for required in src/mobile_sensing/_examples/lausanne.json src/mobile_sensing/_examples/lausanne.zip src/mobile_sensing/_examples/san-francisco.json src/mobile_sensing/_examples/san-francisco.zip; do
  if [ ! -f "$required" ]; then
    echo "Missing release asset: $required" >&2
    exit 1
  fi
done

"$POETRY_EXECUTABLE" run ruff check src/mobile_sensing tests/v2
"$POETRY_EXECUTABLE" run black --check src/mobile_sensing tests/v2
"$POETRY_EXECUTABLE" check --lock
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
"$POETRY_EXECUTABLE" run pytest -q tests/v2
"$POETRY_EXECUTABLE" build

RELEASE_VERSION="$("$POETRY_EXECUTABLE" version -s)"
ASSET_DIRECTORY="release/$RELEASE_VERSION"
mkdir -p "$ASSET_DIRECTORY"
find "$ASSET_DIRECTORY" -mindepth 1 -maxdepth 1 -type f -delete

cp "dist/mobile_sensing-$RELEASE_VERSION-py3-none-any.whl" "$ASSET_DIRECTORY/"
cp "dist/mobile_sensing-$RELEASE_VERSION.tar.gz" "$ASSET_DIRECTORY/"
cp src/mobile_sensing/_examples/lausanne.json "$ASSET_DIRECTORY/"
cp src/mobile_sensing/_examples/lausanne.zip "$ASSET_DIRECTORY/"
cp src/mobile_sensing/_examples/san-francisco.json "$ASSET_DIRECTORY/"
cp src/mobile_sensing/_examples/san-francisco.zip "$ASSET_DIRECTORY/"

(
  cd "$ASSET_DIRECTORY"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum mobile_sensing-* lausanne.json lausanne.zip san-francisco.json san-francisco.zip > SHA256SUMS
  else
    shasum -a 256 mobile_sensing-* lausanne.json lausanne.zip san-francisco.json san-francisco.zip > SHA256SUMS
  fi
)

echo "Release assets: $ASSET_DIRECTORY"
