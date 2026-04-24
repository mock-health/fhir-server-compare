# Local rebuild of Helios FHIR Server's runtime image.
#
# Why this file exists: upstream `ghcr.io/heliossoftware/hfs:0.1.47` (and
# `:latest`) ship a Rust binary linked against GLIBC 2.38+ while packaging it
# on `debian:bookworm-slim`, which provides GLIBC 2.36. The binary fails to
# start on any x86_64 host:
#     /app/hfs: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38'
#     not found (required by /app/hfs)
# Filed as an upstream issue; in the meantime we reuse their prebuilt binary
# (copied out of their own image) but lay it over `debian:trixie-slim` which
# ships GLIBC 2.41. Everything else — workdir, env defaults, entrypoint —
# mirrors upstream exactly so behavior under test matches the published
# binary.
#
# Remove this file (and the `build:` stanza in docker-compose.yml) once
# upstream pins a newer glibc base or statically links musl.

FROM ghcr.io/heliossoftware/hfs:0.1.47 AS source

FROM debian:trixie-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends libssl3 ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 hfs && useradd -u 1000 -g hfs -m hfs

COPY --from=source /app /app
RUN chmod +x /app/hfs

RUN mkdir -p /data && chown hfs:hfs /data
VOLUME /data

WORKDIR /app

ENV HFS_SERVER_HOST=0.0.0.0
ENV HFS_DATABASE_URL=:memory:
ENV BINARY_NAME=hfs

USER hfs
EXPOSE 8080
ENTRYPOINT ["sh", "-c", "exec /app/${BINARY_NAME} \"$@\"", "--"]
