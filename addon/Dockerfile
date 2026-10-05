# Plain Python base (no s6-overlay): the app runs directly as the container
# process, so Docker's init (tini) as PID 1 is correct — no PID-1/s6 conflict.
#
# ---------------------------------------------------------------------------
# Why the base image differs on 32-bit ARM
#
# `cryptography` — AES for media decryption (media/crypto.py) and the broker's
# self-signed cert (mqtt/broker.py) — publishes NO musllinux armv7 wheel. pip
# answers "no matching distribution", so on Alpine a 32-bit build has to compile
# it from Rust source, on the slowest hardware anyone runs this on, from an
# image with no Rust toolchain. It ships `manylinux_2_31_armv7l`, so a glibc
# base simply installs it.
#
# Debian everywhere would be simpler and costs 400 MB: measured 672 MB against
# Alpine's 272 MB for the same app. The base images differ by only ~70 MB — the
# rest is Debian's ffmpeg codec chain. Since almost every install is 64-bit,
# that charges everyone for an arch almost nobody runs. Hence the split.
#
# Keyed on TARGETARCH, which BuildKit sets itself, NOT on the Supervisor's
# BUILD_ARCH. That is deliberate: `build.yaml` and its `build_from` were removed
# in Supervisor 2026.04.0, and the `ARG BUILD_FROM` pattern that replaced it is
# legacy too — the Supervisor substitutes its own base image (one with no Python
# in it) and the build dies with `pip: not found`. Anything we depend on here
# has to come from the build engine, not from the caller.
# ---------------------------------------------------------------------------
# REQUIRES BUILDKIT. `TARGETARCH` is a BuildKit built-in; the legacy builder
# leaves it empty and the build dies on `FROM base-` with "invalid reference
# format". BuildKit is the default from Docker 23 on and is what the Supervisor
# uses, so this only bites a very old daemon — set `DOCKER_BUILDKIT=1` there.
#
# And it MUST stay defaultless. Giving it one (`ARG TARGETARCH=amd64`) looks
# like a safe fallback and is the opposite: BuildKit does not override an ARG
# that already has a default, so every arch — armv7 included — would silently
# get the amd64 base, which is the exact failure this whole block exists to
# prevent. Verified by experiment, not assumed.
ARG TARGETARCH

FROM python:3.14-alpine AS base-amd64
FROM python:3.14-alpine AS base-arm64
FROM python:3.14-slim AS base-arm

FROM base-${TARGETARCH}

WORKDIR /opt/petkit-local

COPY requirements.txt .

# ffmpeg: stream-copy remux of the device's raw .ts clips to .mp4 (see
# media/transcode.py). Missing ffmpeg degrades gracefully (clips stay as .ts),
# but every camera model needs it, so always install it.
#
# Branching on which package manager exists rather than on an arch string: the
# base image is what actually decides this, and it is what we were handed.
#
# On Debian (32-bit ARM) several dependencies publish no armv7 wheel of any libc
# and have to be compiled. What each one needs, taken from the build errors
# rather than guessed:
#
#   psutil, pyyaml (transitive via amqtt)  gcc
#   greenlet       (transitive via SQLAlchemy[asyncio])   g++  — it is C++, and
#                  with only gcc installed pip reports "command 'g++' failed:
#                  No such file or directory"
#   cffi           (transitive via cryptography)   libffi-dev — "fatal error:
#                  ffi.h: No such file or directory"
#
# All small C extensions that build in seconds, unlike cryptography's Rust —
# cryptography itself ships an armv7 wheel, which is the whole reason this branch
# uses a glibc base. The toolchain is purged in the same layer so it never
# reaches the shipped image; `libffi-dev` goes with it, while the libffi runtime
# stays because Python's own `_ctypes` depends on it.
RUN set -eux; \
    if command -v apk >/dev/null 2>&1; then \
        apk add --no-cache ffmpeg; \
        pip install --no-cache-dir -r requirements.txt; \
    else \
        apt-get update; \
        apt-get install -y --no-install-recommends ffmpeg gcc g++ libc6-dev libffi-dev; \
        pip install --no-cache-dir -r requirements.txt; \
        apt-get purge -y gcc g++ libc6-dev libffi-dev; \
        apt-get autoremove -y; \
        rm -rf /var/lib/apt/lists/*; \
    fi

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
# TARGETARCH maps to upstream's names: amd64/arm64 verbatim, and `arm` is their
# armv7 build (statically linked, ARM EABI5 - checked, not assumed).
#
# `wget` exists on Alpine as a busybox applet but NOT in python:*-slim, so the
# Debian branch installs and purges it in the same layer, exactly as it already
# does for the compiler above.
ARG GO2RTC_VERSION=1.9.14
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) sha=32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6 ;; \
        arm64) sha=359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50 ;; \
        arm)   sha=4d7e1639af5a2722a28e864468fd8099b3c1682565446c798bf9e3b38fde12e4 ;; \
        *) echo "no go2rtc build for TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${TARGETARCH}"; \
    if command -v apk >/dev/null 2>&1; then \
        wget -q -O /usr/local/bin/go2rtc "$url"; \
    else \
        apt-get update; \
        apt-get install -y --no-install-recommends wget ca-certificates; \
        wget -q -O /usr/local/bin/go2rtc "$url"; \
        apt-get purge -y wget; \
        apt-get autoremove -y; \
        rm -rf /var/lib/apt/lists/*; \
    fi; \
    echo "${sha}  /usr/local/bin/go2rtc" | sha256sum -c -; \
    chmod +x /usr/local/bin/go2rtc

COPY pyproject.toml .
COPY petkit_local ./petkit_local

ENV PYTHONPATH=/opt/petkit-local \
    PYTHONUNBUFFERED=1

# Self-configures from /data/options.json + the Supervisor MQTT service.
CMD ["python3", "-m", "petkit_local.main", "--ha-addon"]
