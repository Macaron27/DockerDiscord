# syntax=docker/dockerfile:1
# Pinned to the exact interpreter the test suite runs on; bump deliberately.
FROM python:3.14.7-slim-trixie AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt .
# Hash-locked and wheels-only: every artifact is verified and nothing is compiled.
# pip itself is not needed at runtime, so it does not ship.
RUN /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.txt \
 && /opt/venv/bin/pip uninstall --yes pip

FROM python:3.14.7-slim-trixie
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY bot/ bot/
# The root filesystem is read-only at runtime, so compile once here instead of on every start.
RUN python -m compileall -q bot
# Numeric non-root user: needs no /etc/passwd entry.
USER 10001:10001
ENTRYPOINT ["python", "-m", "bot"]
