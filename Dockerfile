FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client tzdata \
    && rm -rf /var/lib/apt/lists/*

# GitHub's published SSH host key (https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)
RUN echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" \
    > /etc/ssh/ssh_known_hosts

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --uid 1000 --create-home brain \
    && mkdir -p /data/brain /data/oauth \
    && chown -R brain:brain /data
USER brain

# Set TZ (e.g. Europe/Paris) at runtime: it dates the "sync: <host> <date>" commits
ENV BRAIN_DIR=/data/brain \
    FASTMCP_HOME=/data/oauth \
    GIT_SSH_COMMAND="ssh -i /run/secrets/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes"

EXPOSE 8000
CMD ["brain-mcp"]
