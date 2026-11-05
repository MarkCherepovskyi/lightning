# syntax=docker/dockerfile:1
#
# Asset channels: an image to run CLN's pytest suite against elementsd on
# Liquid regtest (TEST_NETWORK=liquid-regtest), or against bitcoind on
# Bitcoin regtest, for machines without them (macOS).  No source in the
# image: run-tests.sh next to this file streams the tree in and builds it in
# a volume.

ARG ELEMENTS_VERSION=23.3.4
# As upstream CI (.github/scripts/install-bitcoind.sh).
ARG BITCOIN_VERSION=27.1

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

# Bitcoin Core release binaries, checked the same way.
FROM debian:bookworm-slim AS bitcoin
ARG BITCOIN_VERSION
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/bitcoin
RUN set -e; \
    case "${TARGETARCH}" in \
      amd64) triple=x86_64-linux-gnu ;; \
      arm64) triple=aarch64-linux-gnu ;; \
      *) echo "unsupported architecture ${TARGETARCH}"; exit 1 ;; \
    esac; \
    tarball="bitcoin-${BITCOIN_VERSION}-${triple}.tar.gz"; \
    base="https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_VERSION}"; \
    curl -fsSLO "${base}/${tarball}"; \
    curl -fsSLO "${base}/SHA256SUMS"; \
    grep " ${tarball}\$" SHA256SUMS | sha256sum -c -; \
    tar xzf "${tarball}" --strip-components=1; \
    install -m 755 bin/bitcoind bin/bitcoin-cli /usr/local/bin/

# Build tools, the tests' Python packages, elementsd and bitcoind.
FROM debian:bookworm-slim AS pytest
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential autoconf automake libtool gettext pkg-config git \
      python3 python3-mako python3-venv lowdown jq ca-certificates \
      libsodium-dev libsqlite3-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /venv \
 && /venv/bin/pip install --no-cache-dir \
      'pytest>=8' pytest-xdist pytest-timeout pytest-rerunfailures \
      ephemeral-port-reserve psycopg2-binary python-bitcoinlib jsonschema \
      Flask 'cheroot<=10' psutil requests mnemonic \
      base58 bitstring coincurve cryptography PySocks mako websocket-client
COPY --from=elements /usr/local/bin/elementsd /usr/local/bin/elements-cli /usr/local/bin/
COPY --from=bitcoin /usr/local/bin/bitcoind /usr/local/bin/bitcoin-cli /usr/local/bin/
ENV PATH=/venv/bin:${PATH}
WORKDIR /build
