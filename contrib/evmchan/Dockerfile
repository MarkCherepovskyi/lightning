# syntax=docker/dockerfile:1
#
# EVM channels: an image to run tests/test_evmchan.py (CLN on anvil and the
# UtxoChain contract, SPEC 7) and the plugin's unit tests on Linux, for
# machines without the toolchain (macOS). No source in the image:
# run-tests.sh next to this file streams the tree in and builds CLN and the
# contract in a volume. No bitcoind: the tests run on anvil.

# As on the host (PLAN.md, M1-M3).
ARG FOUNDRY_VERSION=v1.9.0
ARG SOLC_VERSION=0.8.30

# The base image by digest (the multi-arch index bookworm-slim resolved to
# on 2026-11-06).
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS pytest
ARG FOUNDRY_VERSION
ARG SOLC_VERSION
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential autoconf automake libtool gettext pkg-config git curl \
      python3 python3-mako python3-venv lowdown jq ca-certificates \
      libsodium-dev libsqlite3-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*
# Every package pinned, dependencies included (what pip resolved for the
# unpinned list on 2026-11-06).
RUN python3 -m venv /venv \
 && /venv/bin/pip install --no-cache-dir \
      attrs==26.1.0 base58==2.1.1 bitarray==3.12.3 bitstring==4.4.0 blinker==1.9.0 \
      certifi==2026.10.23 cffi==2.1.1 charset-normalizer==3.5.2 cheroot==10.0.0 click==8.5.0 \
      coincurve==21.0.0 cryptography==50.0.2 ephemeral-port-reserve==1.1.4 execnet==2.1.2 \
      Flask==3.1.3 idna==3.20 iniconfig==2.3.1 itsdangerous==2.2.0 jaraco.functools==4.6.0 \
      Jinja2==3.1.6 jsonschema==4.26.0 jsonschema-specifications==2025.9.1 Mako==1.4.3 \
      MarkupSafe==3.0.4 mnemonic==0.21 more-itertools==11.2.1 packaging==26.3 pluggy==1.7.0 \
      psutil==7.2.2 psycopg2-binary==2.9.13 pycparser==3.12 Pygments==2.21.0 PySocks==1.7.1 \
      pytest==9.2.0 pytest-rerunfailures==16.7 pytest-timeout==2.4.0 pytest-xdist==3.8.0 \
      python-bitcoinlib==0.12.2 referencing==0.37.0 requests==2.34.2 rpds-py==2026.9.1 \
      tibs==0.5.7 typing_extensions==4.16.0 urllib3==2.8.0 websocket-client==1.9.2 Werkzeug==3.1.9

# Foundry (anvil, forge, cast): the release tarball for this architecture, checked
# against its sha256 (as GitHub lists it for the v1.9.0 assets). Another
# FOUNDRY_VERSION needs its own hashes here.
RUN arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
 && case "${FOUNDRY_VERSION}/${arch}" in \
      v1.9.0/amd64) sum=8d17599b42e3139f1a9c171be3798c4ed8e743ae17b84cc3435e57bf2a51ae99 ;; \
      v1.9.0/arm64) sum=22d3060fa83515fb901690a2c59a5f552bd41c241886195b2159562fc4947abc ;; \
      *) echo "no sha256 for Foundry ${FOUNDRY_VERSION} on ${arch}" >&2; exit 1 ;; \
    esac \
 && curl -fsSL --retry 5 -o /tmp/foundry.tar.gz \
      "https://github.com/foundry-rs/foundry/releases/download/${FOUNDRY_VERSION}/foundry_${FOUNDRY_VERSION}_linux_${arch}.tar.gz" \
 && echo "${sum}  /tmp/foundry.tar.gz" | sha256sum -c - \
 && mkdir -p /opt/foundry/bin \
 && tar -xzf /tmp/foundry.tar.gz -C /opt/foundry/bin --no-same-owner anvil forge cast \
 && rm -f /tmp/foundry.tar.gz \
 && /opt/foundry/bin/anvil --version && /opt/foundry/bin/forge --version
ENV PATH=/venv/bin:/opt/foundry/bin:${PATH}

# solc for the contract (contracts/foundry.toml pins it), fetched once here
# rather than on every run.
RUN mkdir -p /tmp/solc/src \
 && printf '[profile.default]\nsolc_version = "%s"\noffline = false\n' "${SOLC_VERSION}" > /tmp/solc/foundry.toml \
 && printf '// SPDX-License-Identifier: MIT\npragma solidity %s;\ncontract C {}\n' "${SOLC_VERSION}" > /tmp/solc/src/C.sol \
 && (cd /tmp/solc && forge build) \
 && rm -rf /tmp/solc

WORKDIR /build
