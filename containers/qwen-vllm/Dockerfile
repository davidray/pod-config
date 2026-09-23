# Optional "image mode" (docs/adr/0010). The default "bootstrap mode" runs the
# same entrypoint on the unmodified pinned upstream image, so no registry is
# needed. Build this only if you want the scripts baked in:
#
#   docker build --build-arg VLLM_IMAGE="$(qwenbench infra show a6000 --image)" -t <registry>/qwenbench-vllm:<tag> containers/qwen-vllm
#
# then set `launch_image: <registry>/qwenbench-vllm:<tag>`
# for the model in config/models.yaml (the pod then uses the baked entrypoint).
ARG VLLM_IMAGE
FROM ${VLLM_IMAGE}

COPY entrypoint.sh supervisor.py /opt/qwenbench/
RUN chmod +x /opt/qwenbench/entrypoint.sh
ENTRYPOINT ["/opt/qwenbench/entrypoint.sh"]
