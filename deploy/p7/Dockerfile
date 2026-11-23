# P7 independently provisioned *local authority/recovery* pilot.
# No model/BRAIN SDK dispatch, no Alpha submission, no production grant.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/alpha-research

COPY pyproject.toml README.md ./
COPY alpha_research ./alpha_research

RUN python -m pip install --no-compile . \
    && mkdir -p /run/alpha-research /var/lib/alpha-research \
    && chown -R 65532:65532 /var/lib/alpha-research

USER 65532:65532

# Checks only local secret/PKI custody and read-only SQLite recovery state.
# Does not open any outbound socket or authorize model/BRAIN dispatch.
HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 \
    CMD ["python", "-m", "alpha_research.orchestration.research_authority_server", "--config", "/run/alpha-research/config.json", "--port", "8765", "--require-mtls", "--require-file-secrets", "--check-ready"]

# The service still binds only 127.0.0.1 even with Docker host networking.
ENTRYPOINT ["python", "-m", "alpha_research.orchestration.research_authority_server"]
CMD ["--config", "/run/alpha-research/config.json", "--port", "8765", "--enable-local-pilot", "--require-mtls", "--require-file-secrets"]
