#!/bin/sh
. "$FIXTURE/lock.sh"
. "$FIXTURE/actual.sh"
CASE=${CASE:-$FIXTURE}
RUN=$CASE/runtime
DIR=$CASE/installed
CALENDAR_LIFECYCLE_LOCK=$CASE/lifecycle.flock
CALENDAR_RESOURCE_LOCK=$CASE/resource.flock
CALENDAR_LEGACY_LOCK=$CASE/legacy.lock
export CASE RUN DIR CALENDAR_LIFECYCLE_LOCK CALENDAR_RESOURCE_LOCK CALENDAR_LEGACY_LOCK

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
fixture_now() {
    IFS=' ' read -r fixture_up fixture_rest < /proc/uptime || fail 'fixture uptime unreadable'
    fixture_seconds=${fixture_up%%.*}
    case "$fixture_seconds" in ''|*[!0-9]*) fail 'fixture uptime invalid' ;; esac
}
breadcrumb() {
    IFS=' ' read -r trace_up trace_rest < /proc/uptime || trace_up=unreadable
    printf 'PHASE uptime=%s pid=%s role=%s event=%s\n' \
        "$trace_up" "$$" "${ROLE:-fixture}" "$*" >> "$CASE/phase.$$.log" ||
        fail 'fixture phase log write failed'
}
register() {
    identity "$$" || exit 1
    printf '%s %s\n' "$$" "$proc_start" > "$FIXTURE/pids/$$"
}
wait_file() {
    fixture_now
    wait_started=$fixture_seconds
    wait_ticks=0
    breadcrumb "wait-enter ${1##*/} limit=$FIXTURE_WAIT_LIMIT"
    while [ ! -f "$1" ]; do
        fixture_now
        if [ "$wait_ticks" -ge "$FIXTURE_WAIT_LIMIT" ] ||
            [ "$((fixture_seconds - wait_started))" -ge "$FIXTURE_WAIT_LIMIT" ]; then
            breadcrumb "wait-timeout ${1##*/} elapsed=$((fixture_seconds - wait_started)) ticks=$wait_ticks"
            fail "barrier timeout: ${1##*/} limit=$FIXTURE_WAIT_LIMIT"
        fi
        # Redirect only a forked child, never ash's temporarily saved parent FDs.
        (exec 8>&- 9>&-; command sleep 1)
        wait_ticks=$((wait_ticks + 1))
    done
    breadcrumb "wait-done ${1##*/} ticks=$wait_ticks"
}
barrier() {
    breadcrumb "barrier-ready $1"
    : > "$CASE/$1.ready"
    wait_file "$CASE/$1.go"
    breadcrumb "barrier-released $1"
}
expect() {
    expected=$1
    shift
    if "$@"; then actual=0; else actual=$?; fi
    [ "$actual" = "$expected" ] || fail "expected exit $expected, received $actual"
}
spawn() {
    actor_label=$1
    shift
    /bin/sh "$FIXTURE/actor.sh" "$@" > "$CASE/$actor_label.log" 2>&1 &
    spawned=$!
    identity "$spawned" || fail 'spawned process identity unavailable'
    spawned_start=$proc_start
    (set -C; printf '%s %s\n' "$spawned" "$spawned_start" > "$CASE/$actor_label.spawn") ||
        fail "duplicate spawn identity label: $actor_label"
}
kill_exact() {
    [ "$#" = 2 ] || fail 'kill requires pinned metadata and the expected PID'
    read -r killed_pid killed_start < "$1" || fail 'missing pinned process identity'
    [ "$killed_pid" = "$2" ] || fail 'kill identity differs from the process being waited for'
    alive "$killed_pid" "$killed_start" || fail 'mandatory SIGKILL target is already dead or reused'
    breadcrumb "kill-send pid=$killed_pid start=$killed_start metadata=${1##*/}"
    kill -KILL "$killed_pid" || fail 'SIGKILL delivery failed'
    fixture_now
    killed_began=$fixture_seconds
    killed_ticks=0
    while alive "$killed_pid" "$killed_start"; do
        fixture_now
        [ "$killed_ticks" -lt 5 ] && [ "$((fixture_seconds - killed_began))" -lt 5 ] ||
            fail 'SIGKILL target did not exit within five seconds'
        (exec 8>&- 9>&-; command sleep 1)
        killed_ticks=$((killed_ticks + 1))
    done
    breadcrumb "kill-confirmed pid=$killed_pid start=$killed_start ticks=$killed_ticks"
}
wait_killed() {
    if wait "$1"; then killed_status=0; else killed_status=$?; fi
    [ "$killed_status" = 137 ] || fail "expected SIGKILL wait status 137 for pid=$1, received $killed_status"
    breadcrumb "wait-killed pid=$1 status=$killed_status"
}
await_dead() {
    read -r target_pid target_start < "$1" || fail 'missing process identity'
    fixture_now
    dead_started=$fixture_seconds
    dead_ticks=0
    breadcrumb "await-dead pid=$target_pid limit=$FIXTURE_WAIT_LIMIT"
    while alive "$target_pid" "$target_start"; do
        fixture_now
        [ "$dead_ticks" -lt "$FIXTURE_WAIT_LIMIT" ] &&
            [ "$((fixture_seconds - dead_started))" -lt "$FIXTURE_WAIT_LIMIT" ] ||
            fail "process did not finish: pid=$target_pid limit=$FIXTURE_WAIT_LIMIT"
        (exec 8>&- 9>&-; command sleep 1)
        dead_ticks=$((dead_ticks + 1))
    done
    breadcrumb "dead pid=$target_pid ticks=$dead_ticks"
}
save_identity() {
    identity "$$" || exit 1
    # Role labels are published once; contenders must use per-process labels.
    (set -C; printf '%s %s\n' "$$" "$proc_start" > "$CASE/$1.pid") ||
        fail "duplicate process identity label: $1"
}
diagnose_fixture_pid() {
    case "$1" in "$FIXTURE"/*) ;; *) return 1 ;; esac
    [ -f "$1" ] && [ ! -L "$1" ] || return 1
    read -r diag_pid diag_start < "$1" || return 1
    case "$diag_pid:$diag_start" in *[!0-9:]*|:*|*:) return 1 ;; esac
    # Only inspect an actor registered by this bundle, with its original start.
    [ -f "$FIXTURE/pids/$diag_pid" ] || return 1
    read -r registered_pid registered_start < "$FIXTURE/pids/$diag_pid" || return 1
    [ "$diag_pid:$diag_start" = "$registered_pid:$registered_start" ] || return 1
    if ! identity "$diag_pid"; then
        printf 'PID_DIAG pid=%s recorded_start=%s stat=unreadable-or-exited\n' "$diag_pid" "$diag_start"
        return 0
    fi
    printf 'PID_DIAG pid=%s recorded_start=%s stat_start=%s stat_state=%s\n' \
        "$diag_pid" "$diag_start" "$proc_start" "$proc_state"
    [ "$proc_start" = "$diag_start" ] || return 0
    diag_access=no
    [ ! -r "/proc/$diag_pid/fd" ] || diag_access=yes
    diag_fd9=unreadable
    if readlink "/proc/$diag_pid/fd/9" >/dev/null 2>&1; then diag_fd9=readable; fi
    printf 'FD_DIAG pid=%s directory_readable=%s fd9=%s\n' "$diag_pid" "$diag_access" "$diag_fd9"
    diag_root=$(readlink -f "$FIXTURE") || return 1
    diag_count=0
    for diag_fd in "/proc/$diag_pid/fd/"*; do
        [ "$diag_count" -lt 64 ] || break
        diag_count=$((diag_count + 1))
        diag_target=$(readlink "$diag_fd" 2>/dev/null) || continue
        # Do not print stdio, unrelated paths, or any real task descriptors.
        case "$diag_target" in
            "$diag_root"/*/lifecycle.flock|"$diag_root"/*/resource.flock)
                printf 'LOCK_FD pid=%s fd=%s target=%s\n' "$diag_pid" "${diag_fd##*/}" "$diag_target"
                ;;
        esac
    done
}
log() { :; }
