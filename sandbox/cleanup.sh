#!/bin/sh
# Reclaim disk inside the open-terminal /home volume.
#
# Not installed into the image's startup — shipped as an explicit-action
# script so nothing gets surprise-deleted. Run from the host via cron/timer,
# or `docker compose exec open-terminal /opt/cleanup.sh`.
#
# Env vars (all default to "disabled" — no destructive defaults):
#   CHAT_TTL_DAYS       delete ~/chat-<id>/ dirs whose newest-file mtime
#                       exceeds this many days. 0 = skip.
#   SESSION_TTL_DAYS    delete session JSONLs under
#                       ~/*/.claude/projects/*/*.jsonl older than this
#                       many days. 0 = skip. Note: we now put .claude
#                       *inside* each chat-<id>/, so this is redundant
#                       once CHAT_TTL_DAYS is in use — kept for pre-
#                       migration homes that still have a flat ~/.claude.
#   CLEANUP_DRY_RUN     if "true", print what would be deleted. Default true
#                       the first time you run this — flip explicitly after
#                       reviewing the output.
#
# Exit 0 on normal completion (including dry-run). Non-zero only on hard
# errors (bad env, cannot read /home).

set -eu

CHAT_TTL_DAYS="${CHAT_TTL_DAYS:-0}"
SESSION_TTL_DAYS="${SESSION_TTL_DAYS:-0}"
DRY_RUN="${CLEANUP_DRY_RUN:-true}"

log() { printf "[%s] %s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [ "$CHAT_TTL_DAYS" = 0 ] && [ "$SESSION_TTL_DAYS" = 0 ]; then
    log "both TTLs are 0; nothing to do. Set CHAT_TTL_DAYS and/or SESSION_TTL_DAYS."
    exit 0
fi

log "cleanup start — DRY_RUN=$DRY_RUN CHAT_TTL_DAYS=$CHAT_TTL_DAYS SESSION_TTL_DAYS=$SESSION_TTL_DAYS"

chat_deleted=0
chat_kept=0
session_deleted=0

# Iterate OWUI users. Skip root, distro accounts, etc.
for home in /home/owui_*; do
    [ -d "$home" ] || continue
    user=$(basename "$home")

    # --- chat-<id>/ dirs ---
    if [ "$CHAT_TTL_DAYS" -gt 0 ]; then
        # `find ... -type d` can't filter on newest-file-mtime cheaply;
        # emulate with a per-dir check. One process per user, bounded by
        # #chats (rarely >100).
        for chatdir in "$home"/chat-*; do
            [ -d "$chatdir" ] || continue
            # Most recent mtime of any file *inside* (not the dir itself,
            # which gets touched by subdir creation).
            newest=$(find "$chatdir" -type f -printf '%T@\n' 2>/dev/null | sort -nr | head -1)
            if [ -z "$newest" ]; then
                # Empty chat dir. Treat as old.
                newest=0
            fi
            now=$(date +%s)
            age_days=$(awk "BEGIN { printf \"%d\", ($now - $newest) / 86400 }")
            if [ "$age_days" -gt "$CHAT_TTL_DAYS" ]; then
                if [ "$DRY_RUN" = "true" ]; then
                    log "  would delete $chatdir (idle ${age_days}d)"
                else
                    rm -rf -- "$chatdir"
                    log "  deleted $chatdir (idle ${age_days}d)"
                fi
                chat_deleted=$((chat_deleted + 1))
            else
                chat_kept=$((chat_kept + 1))
            fi
        done
    fi

    # --- legacy flat ~/.claude/projects/ ---
    if [ "$SESSION_TTL_DAYS" -gt 0 ] && [ -d "$home/.claude/projects" ]; then
        # Find JSONLs older than TTL; delete whole parent slug dirs if
        # they're fully aged out.
        while IFS= read -r jsonl; do
            if [ "$DRY_RUN" = "true" ]; then
                log "  would delete $jsonl"
            else
                rm -f -- "$jsonl"
                log "  deleted $jsonl"
            fi
            session_deleted=$((session_deleted + 1))
        done <<EOF
$(find "$home/.claude/projects" -type f -name '*.jsonl' -mtime "+$SESSION_TTL_DAYS" 2>/dev/null)
EOF
    fi
done

log "cleanup done — chat_deleted=$chat_deleted chat_kept=$chat_kept session_deleted=$session_deleted"
