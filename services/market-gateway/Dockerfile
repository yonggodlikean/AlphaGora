# Build context: repository root (Cargo workspace).
FROM rust:1.95-slim AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY services/market-gateway services/market-gateway
RUN cargo build --release --locked -p market-gateway

FROM debian:bookworm-slim
RUN useradd --system --uid 10001 gateway && mkdir -p /data/tape && chown gateway /data/tape
COPY --from=build /src/target/release/market-gateway /usr/local/bin/market-gateway
USER gateway
ENV RUST_LOG=info TAPE_DIR=/data/tape
CMD ["market-gateway"]
