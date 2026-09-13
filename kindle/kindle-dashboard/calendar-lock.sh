#!/bin/sh

CALENDAR_LIFECYCLE_LOCK=/tmp/calendar-dedicated.flock
CALENDAR_RESOURCE_LOCK=/tmp/calendar-auto-refresh.flock
CALENDAR_LEGACY_LOCK=/tmp/calendar-auto-refresh.lock

calendar_lock_require() {
    command -v flock >/dev/null 2>&1 || {
        printf 'LOCK_DEPENDENCY: flock with nonblocking FD support is required; no unsafe fallback.\n' >&2
        return 10
    }
}

calendar_lock_legacy() {
    if [ -e "$CALENDAR_LEGACY_LOCK" ] || [ -L "$CALENDAR_LEGACY_LOCK" ]; then
        printf 'LOCK_LEGACY: old worker lock exists; finish old tasks and reboot before upgrading. Do not delete a live or unowned lock.\n' >&2
        return 32
    fi
}

calendar_lock_path() {
    [ ! -L "$1" ] && { [ ! -e "$1" ] || [ -f "$1" ]; } || {
        printf 'LOCK_UNSAFE: expected a persistent regular lock file; path not touched.\n' >&2
        return 14
    }
}

# A successful flock is the acquisition linearization point. These inodes are
# NEVER removed/replaced. No PID metadata, timeout or reclaimer can unlock them.
# Closing the LAST inherited reference releases the lock, even after SIGKILL.
# Never use flock -u: it would also unlock the open description held by children.
calendar_lock_lifecycle() {
    calendar_lock_path "$CALENDAR_LIFECYCLE_LOCK" || return "$?"
    exec 9>>"$CALENDAR_LIFECYCLE_LOCK" || return 14
    if flock -n 9; then return 0; else calendar_lock_rc=$?; fi
    exec 9>&-
    if [ "$calendar_lock_rc" = 1 ]; then
        printf 'LOCK_BUSY: startup, dedicated mode, recovery or standalone worker still owns the lifecycle; wait, do not delete lock files.\n' >&2
        return 32
    fi
    printf 'LOCK_FAILED: lifecycle flock unavailable; no state changes permitted.\n' >&2
    return 10
}

calendar_lock_has_lifecycle_fd() {
    case "${1-}" in ''|*[!0-9]*) return 1 ;; esac
    # /tmp may be an alias; compare resolved paths, never a guessed mount target.
    if ! calendar_lock_expected=$(readlink -f "$CALENDAR_LIFECYCLE_LOCK") ||
        ! calendar_lock_actual=$(readlink -f "/proc/$1/fd/9") ||
        [ -z "$calendar_lock_expected" ] || [ -z "$calendar_lock_actual" ]; then
        printf 'LOCK_IDENTITY_UNREADABLE: cannot resolve lifecycle file or process FD; refusing handoff or stop.\n' >&2
        return 1
    fi
    [ "$calendar_lock_actual" = "$calendar_lock_expected" ]
}

calendar_lock_inherited() {
    if calendar_lock_has_lifecycle_fd "$$" &&
        flock -n 9; then return 0; fi
    printf 'LOCK_HANDOFF_FAILED: controller, guard, touch and dedicated worker require the inherited lifecycle FD.\n' >&2
    return 32
}

calendar_lock_resource() {
    calendar_lock_legacy || return "$?"
    calendar_lock_path "$CALENDAR_RESOURCE_LOCK" || return "$?"
    exec 8>>"$CALENDAR_RESOURCE_LOCK" || return 14
    if flock -n 8; then return 0; else calendar_lock_rc=$?; fi
    exec 8>&-
    if [ "$calendar_lock_rc" = 1 ]; then
        printf 'LOCK_BUSY: worker, recovery or an inherited command is still using the resource; wait and retry recovery, never remove the lock file.\n' >&2
        return 32
    fi
    printf 'LOCK_FAILED: resource flock unavailable; no state changes permitted.\n' >&2
    return 10
}
