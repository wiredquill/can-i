# syntax=docker/dockerfile:1
# can-i on NVIDIA's base Ubuntu 24.04 image — the default OpenShell sandbox image
# in NVIDIA's Helm chart.  Build from the repo root:
#   docker build -f images/nvidia/Dockerfile -t can-i-nvidia .

# ---- build stage: install the package into a self-contained virtualenv -------
FROM nvcr.io/nvidia/base/ubuntu:24.04 AS builder
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
# a venv pins its own sys.path, so it works whatever env the sandbox runtime keeps
RUN python3 -m venv --without-pip /opt/can-i \
 && python3 -m pip --python /opt/can-i/bin/python install --no-cache-dir --break-system-packages .

# ---- runtime stage --------------------------------------------------------------
FROM nvcr.io/nvidia/base/ubuntu:24.04
LABEL org.opencontainers.image.title="can-i (NVIDIA base Ubuntu 24.04)" \
      org.opencontainers.image.description="Security capability inspector and sandbox demo dashboard (TUI + Web)" \
      org.opencontainers.image.source="https://github.com/wiredquill/can-i"

# curl/getent/unshare/ping/nmap back the default probes
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 curl ca-certificates util-linux iputils-ping nmap \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/can-i /opt/can-i
RUN ln -s /opt/can-i/bin/can-i /usr/local/bin/can-i
COPY checks.yaml /etc/can-i/checks.yaml

RUN useradd --system --uid 10001 --create-home cani
USER 10001
WORKDIR /home/cani

ENV CAN_I_CONFIG=/etc/can-i/checks.yaml
EXPOSE 8000

ENTRYPOINT ["can-i"]
CMD ["web", "--host", "0.0.0.0", "--port", "8000"]
