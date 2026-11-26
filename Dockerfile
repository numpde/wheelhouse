FROM docker.io/library/python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
COPY --from=ghcr.io/astral-sh/uv@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /usr/local/bin/uv
ADD --checksum=sha256:3b29e00006347b558031d5a0c725473b87cf00745ac390a0d0aa8c2e2da275fa https://github.com/numpde/nmr-provider-tools/releases/download/v0.7.7/wheelhouse-tool-0.7.7.pyz /wheelhouse.pyz
ENV PYTHONPATH=/wheelhouse.pyz
WORKDIR /work
