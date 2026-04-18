#!/bin/sh
# Render a Caddyfile picking either OAuth Bearer or x-api-key auth based on
# which env var is set. OAuth wins if both are set (matches the pipe's prior
# precedence). The rendered file lives under /tmp and is never written into
# the image layer.
#
# We use Caddy's {env.X} placeholder for the actual secret so it's loaded at
# runtime, never baked into the config file on disk and never printed. The
# admin API is disabled (admin off) so the config can't be read back over HTTP.
set -eu

if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    # Subscription OAuth token (from `claude setup-token`). Must be sent
    # as Authorization: Bearer, plus the oauth-* anthropic-beta header that
    # tells Anthropic to resolve the Bearer as a subscription token rather
    # than demanding x-api-key.
    #
    # NOTE: we substitute the token into the Caddyfile at render time (shell
    # heredoc) instead of using Caddy's {env.X} placeholder. Empirically the
    # {env.X} form inside a `header_up` directive silently drops the header
    # when the value contains an `=` or other characters in some Caddy
    # versions. Literal substitution is unambiguous. The file lives on
    # tmpfs and isn't readable from outside the container (admin off).
    # `+anthropic-beta` APPENDS rather than replaces. The CLI sends its own
    # beta flags (interleaved-thinking, context-management, etc.) that must
    # be preserved; we only need to add `oauth-2025-04-20` so Anthropic
    # resolves the Bearer as a subscription token.
    AUTH_DIRECTIVE="header_up Authorization \"Bearer ${CLAUDE_CODE_OAUTH_TOKEN}\"
            header_up +anthropic-beta \"oauth-2025-04-20\""
    echo "anthropic-proxy: injecting Authorization (OAuth bearer) + anthropic-beta oauth-2025-04-20"
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    AUTH_DIRECTIVE="header_up x-api-key \"${ANTHROPIC_API_KEY}\""
    echo "anthropic-proxy: injecting x-api-key (API key)"
else
    echo "anthropic-proxy: FATAL — set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY" >&2
    exit 1
fi

cat > /tmp/Caddyfile <<EOF
{
    admin off
    auto_https off
    persist_config off
}

:8081 {
    # Tiny allowlist: only Anthropic API paths. Anything else gets 404'd
    # before a request is forwarded, so the proxy can't be abused as a
    # generic egress relay if the sandbox is compromised.
    @allowed path /v1/*
    handle @allowed {
        reverse_proxy https://api.anthropic.com {
            header_up Host api.anthropic.com
            # Strip any auth the sandbox tries to set so it can't override us.
            # NOTE: in Caddy, `header_up -X` followed by `header_up X val` in
            # the same reverse_proxy block can cancel out (both operations
            # are collapsed per-name). Use replace form `header_up X val`
            # alone — it unconditionally overwrites whatever the client sent.
            header_up -x-api-key
            ${AUTH_DIRECTIVE}
            # flush_interval -1 disables response buffering so Anthropic's
            # SSE stream reaches Claude Code immediately instead of being
            # held in Caddy's default 2s buffer.
            flush_interval -1
            transport http {
                versions 1.1 2
                # Keep upstream connections warm — Anthropic benefits from
                # keep-alive, and new TLS handshakes add measurable latency.
                keepalive 90s
            }
        }
    }
    handle {
        respond "not allowed" 404
    }

    log {
        output stdout
        format console
        # Caddy's access log fields include URI/method/status but never
        # request headers, so the injected credential never hits the log.
    }
}

:8082 {
    # Separate port for health checks — doesn't traverse the auth path.
    handle /healthz {
        respond "ok" 200
    }
    handle {
        respond 404
    }
}
EOF

exec caddy run --config /tmp/Caddyfile --adapter caddyfile
