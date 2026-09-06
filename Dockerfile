# Build the wheel separately so the runtime image carries no build toolchain.
FROM python:3.12-slim AS build

WORKDIR /build
RUN pip install --no-cache-dir build==1.3.0

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist


FROM python:3.12-slim

# Dependencies come from the hash-pinned lock, the package itself without
# resolving anything further, so the image matches what CI tested.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir --no-deps /tmp/*.whl && rm /tmp/*.whl

# The process holds a live XDR token; it has no reason to run as root.
RUN useradd --create-home --uid 10001 xdr
USER xdr

# stdio transport: the MCP client talks to this process over stdin/stdout,
# so the container must be run with -i and must print nothing else to stdout.
ENTRYPOINT ["xdr-mcp"]
