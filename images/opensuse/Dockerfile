# syntax=docker/dockerfile:1
# can-i on openSUSE Leap 16.0 — the same workload as the SUSE BCI image, for
# comparing what a sandbox allows across base images.  Build from the repo root:
#   docker build -f images/opensuse/Dockerfile -t can-i-opensuse .

# ---- build stage: install the package into a self-contained virtualenv -------
FROM registry.opensuse.org/opensuse/leap:16.0 AS builder
RUN zypper -n install --no-recommends python313 python313-pip && zypper -n clean -a
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
# a venv pins its own sys.path, so it works whatever env the sandbox runtime keeps
RUN python3.13 -m venv --without-pip /opt/can-i \
 && python3.13 -m pip --python /opt/can-i/bin/python install --no-cache-dir .

# ---- runtime stage --------------------------------------------------------------
FROM registry.opensuse.org/opensuse/leap:16.0
LABEL org.opencontainers.image.title="can-i (openSUSE Leap 16.0)" \
      org.opencontainers.image.description="Security capability inspector and sandbox demo dashboard (TUI + Web)" \
      org.opencontainers.image.source="https://github.com/wiredquill/can-i"

# curl/getent/unshare/ping back the default probes
RUN zypper -n install --no-recommends python313 curl util-linux shadow iputils && zypper -n clean -a
# nmap isn't in Leap 16.0 itself. The network:utilities project builds it for
# x86_64 only, so on arm64 the nmap check reports "not installed".
ARG TARGETARCH
RUN if [ "$TARGETARCH" = amd64 ]; then \
      zypper -n addrepo https://download.opensuse.org/repositories/network:/utilities/16.0/network:utilities.repo \
   && zypper -n --gpg-auto-import-keys install --no-recommends nmap \
   && zypper -n removerepo network_utilities && zypper -n clean -a; \
    fi
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
