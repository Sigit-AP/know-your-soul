# syntax=docker/dockerfile:1
# soul-knowledge container: deterministic, offline, non-root.
#
# The runtime needs no outbound network (the runtime guard denies provider
# imports, credentials and sockets); network access is only used at build time
# to install dependencies. NOTE: `pip install .` resolves dependency ranges
# from pyproject.toml only — pip does not read uv.lock. For bit-exact locked
# builds, generate a constraints file from uv.lock (`uv export`) or use uv.

FROM python:3.14-slim AS build
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN pip install --no-cache-dir --no-compile .

FROM python:3.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --system soul \
    && useradd --system --gid soul --no-create-home --shell /usr/sbin/nologin soul \
    && mkdir -p /data \
    && chown soul:soul /data
COPY --from=build /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
USER soul
VOLUME ["/data"]
# Default store location inside the container; projections live next to it.
ENV SOUL_STORE=/data/soul.sqlite3
# The `soul-knowledge` console script ships in the package (cli.py) and is
# installed by the build above; e.g.: docker run --rm <image> soul-knowledge --help
ENTRYPOINT ["soul-knowledge"]
