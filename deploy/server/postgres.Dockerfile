# syntax=docker/dockerfile:1
ARG POSTGRES_BASE=postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2
FROM $POSTGRES_BASE AS build

ADD https://github.com/pgvector/pgvector.git#f37c13f68b57d2c3472b2214fbcff699d6d34876 /tmp/pgvector
RUN apk add --no-cache build-base \
    && make -C /tmp/pgvector OPTFLAGS="" with_llvm=no \
    && make -C /tmp/pgvector OPTFLAGS="" with_llvm=no DESTDIR=/stage install

FROM $POSTGRES_BASE
COPY --from=build /stage/usr/local/ /usr/local/
