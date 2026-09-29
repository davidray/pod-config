"""Render a Profile into the exact vLLM argv and Runpod v2 CreatePodRequest.

This is the infrastructure definition: `qwenbench infra render` writes the output
(with secrets replaced by placeholders) to infra/runpod/rendered/, and a test
asserts the committed files match the config, so any drift is caught in CI.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import math
import tarfile
from pathlib import Path
from typing import Any

from qwenbench.config import Profile
from qwenbench.paths import repo_root

BOOTSTRAP_FILES = ("entrypoint.sh", "supervisor.py")
BOOTSTRAP_DIR = "/opt/qwenbench"


def vllm_argv(profile: Profile) -> list[str]:
    m, r = profile.model, profile.runtime
    argv = [
        "vllm", "serve", m.hf_repo,
        "--revision", m.revision,
        "--tokenizer-revision", m.revision,
        "--served-model-name", m.served_model_name,
        "--host", "0.0.0.0",
        "--port", str(profile.port),
        "--max-model-len", str(r.max_model_len),
        "--gpu-memory-utilization", f"{r.gpu_memory_utilization:.2f}",
        "--tensor-parallel-size", str(r.tensor_parallel_size),
        "--kv-cache-dtype", r.kv_cache_dtype,
    ]
    if r.max_num_seqs:
        argv += ["--max-num-seqs", str(r.max_num_seqs)]
    argv.append("--enable-prefix-caching" if r.enable_prefix_caching else "--no-enable-prefix-caching")
    if r.enable_auto_tool_choice:
        argv.append("--enable-auto-tool-choice")
    if r.tool_call_parser:
        argv += ["--tool-call-parser", r.tool_call_parser]
    if r.enable_prompt_tokens_details:
        argv.append("--enable-prompt-tokens-details")
    argv += list(r.extra_args)
    return argv


def container_dir() -> Path:
    return repo_root() / "containers" / "qwen-vllm"


def bootstrap_archive() -> tuple[str, str]:
    """Deterministic base64(tar.gz) of the container scripts + its sha256."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for name in BOOTSTRAP_FILES:
            data = (container_dir() / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    return base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest()


def start_command() -> list[str]:
    return [
        f"set -e; mkdir -p {BOOTSTRAP_DIR}; "
        f'echo "$QWENBENCH_BOOTSTRAP" | base64 -d | tar -xz -C {BOOTSTRAP_DIR}; '
        f"exec bash {BOOTSTRAP_DIR}/entrypoint.sh"
    ]


def pod_env(
    profile: Profile,
    *,
    endpoint_api_key: str,
    list_cost_per_hr: float,
    hf_token: str | None = None,
    self_stop_key: str | None = None,
) -> dict[str, str]:
    archive, digest = bootstrap_archive()

    def secs(v: float | None) -> str:
        return "off" if v is None else str(int(v))

    env = {
        "VLLM_API_KEY": endpoint_api_key,
        "QWENBENCH_PROFILE": profile.name,
        "QWENBENCH_MOUNT": profile.storage.mount_path,
        "QWENBENCH_HF_REPO": profile.model.hf_repo,
        "QWENBENCH_HF_REVISION": profile.model.revision,
        "QWENBENCH_VLLM_ARGV": json.dumps(vllm_argv(profile)),
        "QWENBENCH_VLLM_PORT": str(profile.port),
        "QWENBENCH_WATCHDOG_PORT": str(profile.watchdog_port),
        "QWENBENCH_IDLE_TIMEOUT_S": secs(profile.idle_timeout_s),
        "QWENBENCH_STARTUP_TIMEOUT_S": secs(profile.startup_timeout_s),
        "QWENBENCH_MAX_SESSION_S": secs(profile.max_session_s),
        "QWENBENCH_MAX_SPEND_USD": "off" if profile.max_spend_usd is None else f"{profile.max_spend_usd:.2f}",
        "QWENBENCH_LIST_COST_PER_HR": f"{list_cost_per_hr:.4f}",
        "QWENBENCH_BOOTSTRAP": archive,
        "QWENBENCH_BOOTSTRAP_SHA256": digest,
    }
    if hf_token:
        env["HF_TOKEN"] = hf_token
    if self_stop_key:
        env["QWENBENCH_SELF_STOP_KEY"] = self_stop_key
    return env


def create_body(
    profile: Profile,
    *,
    env: dict[str, str],
    data_center_ids: list[str],
    network_volume_id: str | None,
) -> dict[str, Any]:
    proto = "http" if profile.endpoint_mode == "proxy" else "tcp"
    body: dict[str, Any] = {
        "name": profile.pod_name,
        "image": profile.model.launch_image or profile.model.image,
        "env": env,
        "ports": [f"{profile.port}/{proto}", f"{profile.watchdog_port}/{proto}"],
        "cloud": profile.cloud,
        "gpu": {
            "id": profile.gpu_type_id,
            "count": profile.gpu_count,
            "minCudaVersion": profile.model.cuda_version,
        },
        "dataCenterIds": data_center_ids,
    }
    if not profile.model.launch_image:
        body["entrypoint"] = ["/bin/bash", "-c"]
        body["cmd"] = start_command()
    else:
        body["env"] = {k: v for k, v in env.items() if k != "QWENBENCH_BOOTSTRAP"}
    if profile.storage.mode == "network-volume":
        if not network_volume_id:
            raise ValueError("network-volume storage requires a resolved volume id")
        body["disk"] = profile.container_disk_gb
        body["mounts"] = {"network": [{"volumeId": network_volume_id, "path": profile.storage.mount_path}]}
    else:
        # Ephemeral: weights live on the container disk and are re-downloaded
        # on every pod. Size for the weights + 20% headroom, not the volume
        # size: oversized disk requests narrow the set of hosts that can place us.
        body["disk"] = profile.container_disk_gb + math.ceil(profile.model.size_gb * 1.2)
    return body


SECRET_ENV = ("VLLM_API_KEY", "HF_TOKEN", "QWENBENCH_SELF_STOP_KEY")


def rendered_for_repo(body: dict[str, Any]) -> dict[str, Any]:
    """Placeholder secrets and the bulky bootstrap blob for committing."""
    out = json.loads(json.dumps(body))
    for k in SECRET_ENV:
        if k in out.get("env", {}):
            out["env"][k] = f"${{{k}}}"
    if "QWENBENCH_BOOTSTRAP" in out.get("env", {}):
        out["env"]["QWENBENCH_BOOTSTRAP"] = "<base64 tar.gz of containers/qwen-vllm; see QWENBENCH_BOOTSTRAP_SHA256>"
    for mount in out.get("mounts", {}).get("network", []):
        mount["volumeId"] = "<discovered by name at `qwenbench up`>"
    return out
