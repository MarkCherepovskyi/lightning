# syntax=docker/dockerfile:1
#
# Swaps between Liquid and EVM channels (assetswap-plugin): an image with
# elementsd (Liquid regtest), anvil and forge (the EVM UtxoChain), and the
# Rust toolchain for the plugins.  It is the two test images of
# contrib/assetchan and contrib/evmchan put together, so build those first
# (their run-tests.sh does); run-tests.sh next to this file does it for you.
# No source in the image: run-tests.sh streams the tree in and builds it in
# a volume.

ARG ASSETCHAN_IMAGE=assetchan/pytest
ARG EVMCHAN_IMAGE=evmchan/pytest
# The workspace's rust-version (Cargo.toml).
ARG RUST_VERSION=1.85.0

FROM ${EVMCHAN_IMAGE} AS evm

FROM ${ASSETCHAN_IMAGE} AS pytest
ARG RUST_VERSION
# anvil, forge, cast, and the solc forge already fetched.
COPY --from=evm /opt/foundry /opt/foundry
COPY --from=evm /root/.svm /root/.svm
COPY --from=evm /root/.foundry /root/.foundry
# (Downloaded to a file first: `curl | sh` succeeds on a failed download.)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && curl --proto '=https' --tlsv1.2 -fsSL --retry 5 -o /tmp/rustup-init.sh https://sh.rustup.rs \
 && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain "${RUST_VERSION}" \
 && rm /tmp/rustup-init.sh \
 && /root/.cargo/bin/cargo --version
ENV PATH=/venv/bin:/root/.cargo/bin:/opt/foundry/bin:${PATH}
WORKDIR /build
