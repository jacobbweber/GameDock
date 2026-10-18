# OPTIONAL: run GameDock itself inside a container. The recommended way is the systemd/venv route (see deploy/).
# Read "Security" in docs/DEPLOYMENT.md first: this setup hands GameDock the Docker socket, which is
# equivalent to root access on the host. This file has not been built on the project's test machine (no Docker there).

# Copy only the docker command-line client (about 30 MB), not a whole engine.
FROM docker:27-cli AS dockercli

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY gamedock ./gamedock
RUN pip install .
COPY catalog ./catalog
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

# Inside a container GameDock must listen on all addresses; docker-compose.yml publishes the port on the host's
# 127.0.0.1 only, which is what keeps it private.
ENV GAMEDOCK_BACKEND=docker \
    GAMEDOCK_HOST=0.0.0.0 \
    GAMEDOCK_PORT=8080 \
    GAMEDOCK_CATALOG_DIR=/app/catalog
EXPOSE 8080

# The user is set in docker-compose.yml (your own uid, plus the docker group) so files in the data folder
# belong to you on the host, not to a mystery user.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('GAMEDOCK_PORT','8080'), timeout=4)"
CMD ["python", "-m", "gamedock"]
