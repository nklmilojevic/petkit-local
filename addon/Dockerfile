# Plain Python base (no s6-overlay): the app runs directly as the container
# process, so Docker's init (tini) as PID 1 is correct — no PID-1/s6 conflict.
#
# 64-bit only: amd64 and arm64 (aarch64). Both have musllinux wheels for every
# dependency, so a single Alpine base installs everything without compiling.
#
# Not keyed on the Supervisor's BUILD_ARCH or `ARG BUILD_FROM`: `build.yaml` and
# its `build_from` were removed in Supervisor 2026.04.0, and the Supervisor now
# substitutes its own base image (one with no Python in it), so the build dies
# with `pip: not found`. The base is hardcoded here instead.
FROM python:3.14-alpine

WORKDIR /opt/petkit-local

COPY requirements.txt .

# ffmpeg: stream-copy remux of the device's raw .ts clips to .mp4 (see
# media/transcode.py). Missing ffmpeg degrades gracefully (clips stay as .ts),
# but every camera model needs it, so always install it.
RUN set -eux; \
    apk add --no-cache ffmpeg; \
    pip install --no-cache-dir -r requirements.txt

# go2rtc: the camera sidecar (media/go2rtc.py). It is the ONLY safe way to give
# a device camera to Home Assistant — HA opens a stream URL with PyAV, and the
# device's own FLV segfaults libav and restarts the whole of HA, while tserver
# additionally refuses every second connection. go2rtc holds one connection and
# republishes RTSP, which HA reads happily.
#
# Pinned and checksummed rather than :latest, because this is a binary we hand a
# device's video to and the build must be reproducible. Upstream ships static
# builds (4.4-5.5 MB) so there is nothing to compile on any arch.
#
# TARGETARCH maps to upstream's names verbatim (amd64, arm64). It REQUIRES
# BUILDKIT, which sets it: the legacy builder leaves it empty. And it MUST stay
# defaultless — BuildKit does not override an ARG that already has a default, so
# `ARG TARGETARCH=amd64` would hand every arch the amd64 binary.
ARG GO2RTC_VERSION=1.9.14
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) sha=32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6 ;; \
        arm64) sha=359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50 ;; \
        *) echo "no go2rtc build for TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    wget -q -O /usr/local/bin/go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${TARGETARCH}"; \
    echo "${sha}  /usr/local/bin/go2rtc" | sha256sum -c -; \
    chmod +x /usr/local/bin/go2rtc

COPY pyproject.toml .
COPY petkit_local ./petkit_local

ENV PYTHONPATH=/opt/petkit-local \
    PYTHONUNBUFFERED=1

# Self-configures from /data/options.json + the Supervisor MQTT service.
CMD ["python3", "-m", "petkit_local.main", "--ha-addon"]
