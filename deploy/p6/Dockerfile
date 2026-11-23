# P6.1 independently operated LOCAL PILOT only.
# This image has no model/BRAIN credentials, no production dispatch route.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/alpha-research

COPY pyproject.toml README.md ./
COPY alpha_research ./alpha_research

RUN python -m pip install --no-compile . \
    && mkdir -p /run/alpha-research /var/lib/alpha-research \
    && chown -R 65532:65532 /run/alpha-research /var/lib/alpha-research

USER 65532:65532

# Runtime PEMs and JSON are operator-provisioned mounted files.
# The service binds *only* to 127.0.0.1 inside this container.
# Use --network none for fully isolated offline acceptance.
ENTRYPOINT ["python", "-m", "alpha_research.orchestration.research_authority_server"]
CMD ["--config", "/run/alpha-research/config.json", "--port", "8765", "--enable-local-pilot"]
