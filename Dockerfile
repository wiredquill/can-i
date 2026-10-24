# syntax=docker/dockerfile:1
# can-i — built strictly on SUSE Base Container Images (BCI), multi-stage.

# ---- build stage: install the package into an isolated prefix ----------------
FROM registry.suse.com/bci/python:3.13 AS builder
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

# ---- runtime stage: minimal, non-root -----------------------------------------
FROM registry.suse.com/bci/python:3.13
LABEL org.opencontainers.image.title="can-i" \
      org.opencontainers.image.description="Security capability inspector and sandbox demo dashboard (TUI + Web)" \
      org.opencontainers.image.source="https://github.com/wiredquill/can-i"

COPY --from=builder /install /usr/local
COPY checks.yaml /etc/can-i/checks.yaml

# Run as an unprivileged user — the whole point is probing what is denied.
RUN useradd --system --uid 10001 --create-home cani
USER 10001
WORKDIR /home/cani

ENV CAN_I_CONFIG=/etc/can-i/checks.yaml
EXPOSE 8000

ENTRYPOINT ["can-i"]
CMD ["web", "--host", "0.0.0.0", "--port", "8000"]
