FROM docker.io/library/python:3.13.18-slim-bookworm@sha256:f0fa218d5b06f9b9235024fa061cde2cb141f726de52134471053d2d9b5324a3
COPY --from=ghcr.io/astral-sh/uv:0.10.0@sha256:78a7ff97cd27b7124a5f3c2aefe146170793c56a1e03321dd31a289f6d82a04f /uv /usr/local/bin/uv
COPY requirements.txt /tmp/requirements.txt
RUN uv --no-config --no-cache pip install --system --no-python-downloads \
    --only-binary=:all: --require-hashes -r /tmp/requirements.txt
ENV PYTHONPATH=/opt/wheelhouse PYTHONDONTWRITEBYTECODE=1
COPY wheelhouse.py /opt/wheelhouse/wheelhouse.py
WORKDIR /work
ENTRYPOINT ["python", "/opt/wheelhouse/wheelhouse.py"]
