#!/usr/bin/env bash
# qwenbench pod entrypoint. Runs inside the pinned vllm/vllm-openai image.
#
# Delivered one of two ways (same script either way, see docs/adr/0010):
#   bootstrap mode: the CLI ships this directory as base64 tar in env
#                   QWENBENCH_BOOTSTRAP and the pod start command unpacks it
#   image mode:     containers/qwen-vllm/Dockerfile bakes it into an image
set -euo pipefail

MOUNT="${QWENBENCH_MOUNT:-/workspace}"
export HF_HOME="${HF_HOME:-$MOUNT/hf}"
export QWENBENCH_STATE_ROOT="${QWENBENCH_STATE_ROOT:-$MOUNT/qwenbench}"
mkdir -p "$HF_HOME" "$QWENBENCH_STATE_ROOT/logs" "$MOUNT/vllm-cache"

# Persist torch.compile / CUDA graph artifacts across pods.
mkdir -p "$HOME/.cache"
if [ ! -L "$HOME/.cache/vllm" ]; then
  rm -rf "$HOME/.cache/vllm"
  ln -s "$MOUNT/vllm-cache" "$HOME/.cache/vllm"
fi

# If the pinned revision is already on the volume, skip every Hub round-trip.
repo_dir="$HF_HOME/hub/models--${QWENBENCH_HF_REPO//\//--}"
if [ -n "${QWENBENCH_HF_REVISION:-}" ] && [ -d "$repo_dir/snapshots/$QWENBENCH_HF_REVISION" ] \
   && ! find "$repo_dir/blobs" -name '*.incomplete' 2>/dev/null | grep -q .; then
  echo "[qwenbench] model revision $QWENBENCH_HF_REVISION found in cache; HF_HUB_OFFLINE=1"
  export HF_HUB_OFFLINE=1
else
  echo "[qwenbench] model revision not cached; vLLM will download to $HF_HOME"
fi

nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader || true
python3 -c 'import vllm, torch; print("[qwenbench] vllm", vllm.__version__, "torch", torch.__version__, "cuda", torch.version.cuda)' || true

exec python3 "$(dirname "$0")/supervisor.py"
