#!/bin/zsh
# corridor-par.sh — lock-based parallel worker for corridor-v2 remaining boxes.
# Launch N copies; each pulls the next unlocked, unbuilt box.
#
# PATH matters: hillgen shells out to gdalsrsinfo (GDAL CLI). tmux-launched zsh
# lacks /opt/homebrew/bin and every box dies with FileNotFoundError without this.
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
export HILLGEN_CACHE=/Volumes/mapdrive/hillgen-cache
export TMPDIR=/Volumes/mapdrive/tmp

# CRITICAL: drop the gateway egress-proxy environment.
# Anything launched from an OpenClaw shell session inherits
# HTTP(S)_PROXY=http://<token>@127.0.0.1:51851 with a per-session credential.
# The token dies when that exec session ends, so a long-running job is left
# holding a credential that goes stale under it and every DEM fetch then fails
# with "curl: (56) CONNECT tunnel failed, response 407". That is the 407 storm
# that killed wide01-wide08 on Nov 10 and wide04/05 on Nov 11.
# Direct egress from this host works (DOGAMI and 3DEP both return 200), so
# hillgen must never go through the proxy.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
unset NODE_EXTRA_CA_CERTS NODE_USE_ENV_PROXY
unset SSL_CERT_FILE CURL_CA_BUNDLE REQUESTS_CA_BUNDLE GIT_SSL_CAINFO

OUT=/Volumes/mapdrive/hillgen-out/corridor-v2
LOCKS=/Volumes/mapdrive/hillgen-out/locks
LOG=/Volumes/mapdrive/hillgen-out/corridor-par.log
RANGE="0,1400"
MIN_FREE_GB=40
W="${1:-w?}"
mkdir -p "$LOCKS" "$TMPDIR"
cd /Users/muehlbucks/hillshade-generator || exit 1

# name bbox
BOXES=(
 "wide04 -124.3796,43.9570,-123.8037,45.0185"
 "wide05 -124.6805,42.9248,-123.8791,44.3174"
 "wide06 -124.6784,41.9998,-124.1144,43.2852"
 "wide07 -124.6004,41.1978,-123.7829,42.3602"
 "wide08 -124.4310,40.1328,-123.6627,41.5582"
 "wide09 -124.1438,39.2456,-123.5687,40.4932"
 "wide11 -122.8002,37.4411,-121.7717,38.0762"
)

echo "[$W] worker start $(date)" >> "$LOG"
for entry in "${BOXES[@]}"; do
  name="${entry%% *}"; bbox="${entry##* }"

  # already built?
  if ls "$OUT/$name"/*.mbtiles >/dev/null 2>&1; then continue; fi
  # atomic claim
  if ! mkdir "$LOCKS/$name" 2>/dev/null; then continue; fi
  echo "$W $$ $(date)" > "$LOCKS/$name/owner"

  avail=$(df -g /Volumes/mapdrive | awk 'NR==2{print $4}')
  if [ "$avail" -lt "$MIN_FREE_GB" ]; then
    echo "[$W] ABORT: only ${avail}GB free (floor $MIN_FREE_GB) $(date)" >> "$LOG"
    rmdir "$LOCKS/$name" 2>/dev/null; exit 9
  fi

  rc=1
  for attempt in 1 2 3; do
    [ "$attempt" -gt 1 ] && { echo "[$W] RETRY $name attempt $attempt (prev exit=$rc) $(date)" >> "$LOG"; sleep 60; }
    echo "[$W] BEGIN $name $bbox z5-13 attempt $attempt $(date)" >> "$LOG"
    ./.venv/bin/hillgen run \
      --bbox "$bbox" \
      --theme topo-classic \
      --exaggeration 9 \
      --elev-range "$RANGE" \
      --zoom 5-13 \
      --dem auto \
      --buffer 0 \
      --no-contribute \
      --output "$OUT/$name" \
      --yes >> "$LOG" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] && break
  done
  echo "[$W] END $name exit=$rc $(date)" >> "$LOG"
  [ "$rc" -ne 0 ] && rm -rf "$LOCKS/$name" 2>/dev/null
done
echo "[$W] worker done $(date)" >> "$LOG"
