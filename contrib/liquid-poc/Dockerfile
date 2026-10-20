# syntax=docker/dockerfile:1
#
# Liquid asset channel PoC: an elementsd image and a Core Lightning image built
# from this source tree.  Build context is the repository root; see
# docker-compose.yml next to this file.

ARG ELEMENTS_VERSION=23.3.4

# Elements release binaries, checked against the release's SHA256SUMS.
FROM debian:bookworm-slim AS elements
ARG ELEMENTS_VERSION
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/elements
RUN set -e; \
    case "${TARGETARCH}" in \
      amd64) triple=x86_64-linux-gnu ;; \
      arm64) triple=aarch64-linux-gnu ;; \
      *) echo "unsupported architecture ${TARGETARCH}"; exit 1 ;; \
    esac; \
    tarball="elements-${ELEMENTS_VERSION}-${triple}.tar.gz"; \
    base="https://github.com/ElementsProject/elements/releases/download/elements-${ELEMENTS_VERSION}"; \
    curl -fsSLO "${base}/${tarball}"; \
    curl -fsSLO "${base}/SHA256SUMS"; \
    grep " ${tarball}\$" SHA256SUMS | sha256sum -c -; \
    tar xzf "${tarball}" --strip-components=1; \
    install -m 755 bin/elementsd bin/elements-cli /usr/local/bin/

# The Liquid regtest node.
FROM debian:bookworm-slim AS elementsd
COPY --from=elements /usr/local/bin/elementsd /usr/local/bin/elements-cli /usr/local/bin/
VOLUME /data
ENTRYPOINT ["elementsd"]

# Core Lightning from this source tree (the liquid branch).  Rust plugins are
# left out to keep the build short.
FROM debian:bookworm-slim AS cln-builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential autoconf automake libtool gettext pkg-config git \
      python3 python3-mako lowdown jq ca-certificates \
      libsodium-dev libsqlite3-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
ARG VERSION=liquid-poc
# PYTHON is set so configure doesn't go looking for uv.
RUN PYTHON=python3 ./configure --prefix=/opt/lightning --disable-rust --disable-valgrind --disable-compat \
 && make -j"$(nproc)" VERSION="${VERSION}" install-program

FROM debian:bookworm-slim AS cln
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libsodium23 libsqlite3-0 zlib1g python3 jq \
 && rm -rf /var/lib/apt/lists/*
COPY --from=cln-builder /opt/lightning /opt/lightning
# bcli talks to elementsd through elements-cli.
COPY --from=elements /usr/local/bin/elements-cli /usr/local/bin/
ENV PATH=/opt/lightning/bin:${PATH}
VOLUME /data
ENTRYPOINT ["lightningd"]
