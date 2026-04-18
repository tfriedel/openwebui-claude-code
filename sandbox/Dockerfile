FROM ghcr.io/open-webui/open-terminal:latest

# Pin claude-code CLI so two clean builds from the same source produce
# identical behaviour. Override at build time:
#   docker compose build --build-arg CLAUDE_CODE_VERSION=X.Y.Z open-terminal
# When bumping the default here, keep the image rebuild + test cycle
# together — the stream-json schema can change between versions.
ARG CLAUDE_CODE_VERSION=2.1.114

# Base image runs as uid=1000(user) with npm prefix /usr, which requires
# root to write. Switch to root for the global install, then back so the
# container's runtime user and entrypoint stay identical to upstream.
USER root
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && claude --version

# bubblewrap + socat are the Linux backends for Claude Code's built-in
# `/sandbox` feature. Without them, Claude falls back to unsandboxed bash —
# our managed-settings.json sets failIfUnavailable=true so that silent
# degradation becomes a hard error instead.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap socat \
    && rm -rf /var/lib/apt/lists/*

# Managed (enterprise) settings live at the system-wide path and take
# precedence over per-user and per-project settings. This is how we pin
# Bash egress + WebFetch allowlists: users can extend via their own
# settings, but cannot downgrade these rules.
COPY managed-settings.json /etc/claude-code/managed-settings.json
RUN chmod 0644 /etc/claude-code/managed-settings.json

COPY cleanup.sh /opt/cleanup.sh
RUN chmod +x /opt/cleanup.sh
USER user
