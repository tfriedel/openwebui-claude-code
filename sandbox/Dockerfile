FROM ghcr.io/open-webui/open-terminal:latest

# Base image runs as uid=1000(user) with npm prefix /usr, which requires
# root to write. Switch to root for the global install, then back so the
# container's runtime user and entrypoint stay identical to upstream.
USER root
RUN npm install -g @anthropic-ai/claude-code@latest \
    && claude --version
USER user
