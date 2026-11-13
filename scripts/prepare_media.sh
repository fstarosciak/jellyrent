#!/usr/bin/env bash
# Generates short synthetic films and distributes them over the three nodes.
#
# Titles deliberately overlap between nodes - the same title on several nodes
# means several independent copies ("discs"):
#
#   Film              jf1  jf2  jf3
#   Big Buck Bunny     x    x    x
#   Sintel             x    x
#   Tears of Steel          x    x
#   Elephants Dream    x
#   Spring                       x
#
# Folder names follow Jellyfin's convention "Name (Year) [imdbid-tt...]", so
# every node assigns the same IMDb id without internet access - that is what
# the catalog sync matches on. Spring has no id on purpose: it exercises the
# name + year fallback.
#
# Uses a local ffmpeg if present, otherwise the ffmpeg bundled in the pinned
# Jellyfin image (no extra download). DURATION=seconds overrides the length.
set -euo pipefail
cd "$(dirname "$0")/.."

DURATION="${DURATION:-120}"
JELLYFIN_IMAGE="$(grep -m1 -oE 'jellyfin/jellyfin:[^[:space:]"]+' docker-compose.yml)"
SRC_DIR="media/.src"

#        title             | year | imdb id   | lavfi pattern | nodes
MOVIES=(
  "Big Buck Bunny          |2008|tt1254207 |testsrc2      |1 2 3"
  "Sintel                  |2010|tt1727587 |smptehdbars   |1 2"
  "Tears of Steel          |2012|tt2285752 |mandelbrot    |2 3"
  "Elephants Dream         |2006|tt0807840 |testsrc       |1"
  "Spring                  |2019|          |rgbtestsrc    |3"
)

trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; echo "${s%"${s##*[![:space:]]}"}"; }

run_ffmpeg() {  # args use paths relative to ./media
  if command -v ffmpeg >/dev/null 2>&1; then
    (cd media && ffmpeg -hide_banner -loglevel error "$@")
  else
    docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/media:/work" -w /work \
      --entrypoint /usr/lib/jellyfin-ffmpeg/ffmpeg "$JELLYFIN_IMAGE" \
      -hide_banner -loglevel error "$@"
  fi
}

mkdir -p "$SRC_DIR" media/jf1 media/jf2 media/jf3

for row in "${MOVIES[@]}"; do
  IFS='|' read -r title year imdb pattern nodes <<<"$row"
  title="$(trim "$title")"; imdb="$(trim "$imdb")"; pattern="$(trim "$pattern")"
  folder="$title ($year)"
  [[ -n "$imdb" ]] && folder="$folder [imdbid-$imdb]"
  source_file="$SRC_DIR/$title ($year).mp4"

  if [[ ! -s "$source_file" ]]; then
    echo "generating: $title ($year) - ${DURATION}s"
    run_ffmpeg -y \
      -f lavfi -i "${pattern}=size=640x360:rate=24" \
      -f lavfi -i "sine=frequency=440:sample_rate=48000" \
      -t "$DURATION" -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -shortest \
      -metadata title="$title" -metadata date="$year" \
      ".src/$title ($year).mp4"
  fi

  for node in $nodes; do
    target="media/jf$node/$folder"
    mkdir -p "$target"
    cp -f "$source_file" "$target/$title ($year).mp4"
    echo "  -> jf$node: $folder"
  done
done

echo "Media ready under ./media (jf1, jf2, jf3)."
