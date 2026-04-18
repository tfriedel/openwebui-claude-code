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
# degradation becomes a hard error instead. git is needed for the skill
# vendoring step below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap socat git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Native deps for the document skills:
#   libreoffice-{core,writer,calc,impress} — headless `soffice` for .doc↔.docx
#     conversion, tracked-change acceptance, xlsx recalc, and pptx thumbs.
#   poppler-utils — pdftoppm binary + pdf2image Python backend.
#   fonts-dejavu — baseline sans/serif so rendered PDFs aren't blank boxes.
# The LibreOffice subpackages total ~500 MB; installing the core meta
# package would add Java + GUI deps we never use. Pandoc (docx→markdown
# reading path) is already in the base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-core libreoffice-writer libreoffice-calc \
        libreoffice-impress \
        poppler-utils fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

# Python libraries used by the skill scripts + standard data-science
# tooling so ad-hoc analysis doesn't require a per-turn `pip install`
# (which would exercise the egress allowlist every time and slow the
# first response). Installed system-wide into the base image's Python
# 3.12 under /usr/local (not PEP 668 managed, so no --break-system
# flag needed).
RUN pip3 install --no-cache-dir \
        pypdf pdfplumber pdf2image reportlab Pillow \
        openpyxl python-docx python-pptx \
        pandas numpy matplotlib \
        lxml defusedxml \
        'markitdown[pptx]'

# Node package for the docx skill's "Creating New Documents" path —
# docx-js is preferred over python-docx when generating richly formatted
# documents from scratch (tables of contents, footnotes, columns, etc.).
RUN npm install -g docx

# Vendor a curated set of Anthropic skills into /opt/claude-skills. Each
# chat's CLAUDE_CONFIG_DIR/.claude/skills/ symlinks here at runtime, so all
# users share a read-only copy without per-chat duplication. To update the
# set, change the sparse-checkout list and rebuild — running chats pick up
# the new set on their next turn (symlinks re-point).
ARG CLAUDE_SKILLS="docx pdf pptx xlsx frontend-design"
RUN set -eux; \
    mkdir -p /opt/claude-skills; \
    tmp="$(mktemp -d)"; \
    git clone --depth 1 --filter=blob:none --sparse \
        https://github.com/anthropics/skills.git "$tmp"; \
    cd "$tmp"; \
    paths=""; for s in $CLAUDE_SKILLS; do paths="$paths skills/$s"; done; \
    git sparse-checkout set $paths; \
    for s in $CLAUDE_SKILLS; do \
        cp -r "skills/$s" "/opt/claude-skills/$s"; \
    done; \
    cd /; rm -rf "$tmp"; \
    ls /opt/claude-skills/

# Managed (enterprise) settings live at the system-wide path and take
# precedence over per-user and per-project settings. This is how we pin
# Bash egress + WebFetch allowlists: users can extend via their own
# settings, but cannot downgrade these rules.
COPY managed-settings.json /etc/claude-code/managed-settings.json
RUN chmod 0644 /etc/claude-code/managed-settings.json

COPY cleanup.sh /opt/cleanup.sh
RUN chmod +x /opt/cleanup.sh
USER user
