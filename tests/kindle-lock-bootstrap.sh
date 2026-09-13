#!/bin/sh
# Generated bundles contain only extracted functions/entry blocks and synthetic data.
set -eu
case "$(uname -s)" in Linux) ;; *) echo 'SKIP requires Linux flock and /proc'; exit 77 ;; esac
for tool in flock readlink setsid nohup mktemp mkdir cp rm rmdir sleep awk cat date mv ln tail; do
    command -v "$tool" >/dev/null 2>&1 || { echo "SKIP missing $tool"; exit 77; }
done
[ -r /proc/self/stat ] || { echo 'SKIP requires Linux /proc'; exit 77; }
umask 077
exec 8>&- 9>&-
FIXTURE=$(mktemp -d /tmp/kindle-lock-fixture.XXXXXX) || exit 1
export FIXTURE
FIXTURE_WAIT_LIMIT=90
FIXTURE_SUITE_LIMIT=900
export FIXTURE_WAIT_LIMIT FIXTURE_SUITE_LIMIT
mkdir "$FIXTURE/pids"
# Cleanup is installed before extracting any assets. Later cleanup uses actual identity().
cleanup_fixture() {
    rc=$?
    trap - 0
    if [ "$rc" != 0 ]; then
        failed_case=${CASE:-bootstrap}
        printf 'FAILED_CASE=%s exit=%s\n' "${failed_case##*/}" "$rc"
        case "${CASE-}" in
            "$FIXTURE"|"$FIXTURE"/*)
                if command -v diagnose_fixture_pid >/dev/null 2>&1; then
                    diag_metadata_count=0
                    for diag_metadata in "$CASE/runtime/owner" "$CASE/runtime/guard" "$CASE"/*.spawn "$CASE"/*.pid; do
                        [ -f "$diag_metadata" ] && [ ! -L "$diag_metadata" ] || continue
                        [ "$diag_metadata_count" -lt 12 ] || break
                        diagnose_fixture_pid "$diag_metadata" || printf 'PID_DIAG metadata unavailable or not registered\n'
                        diag_metadata_count=$((diag_metadata_count + 1))
                    done
                fi
                log_count=0
                for fixture_log in "$CASE/runtime/console.log" "$CASE/controller.exit" "$CASE"/phase.*.log "$CASE"/*.log; do
                    [ -f "$fixture_log" ] && [ ! -L "$fixture_log" ] || continue
                    case "$fixture_log" in
                        "$CASE"/phase.*.log)
                            case " ${diagnosed_phases-} " in *" $fixture_log "*) continue ;; esac
                            diagnosed_phases="${diagnosed_phases-} $fixture_log"
                            ;;
                    esac
                    [ "$log_count" -lt 12 ] || break
                    printf 'FIXTURE_LOG=%s (last 2048 bytes)\n' "${fixture_log#"$CASE/"}"
                    tail -c 2048 "$fixture_log" || printf 'FIXTURE_LOG_UNREADABLE\n'
                    printf '\n'
                    log_count=$((log_count + 1))
                done
                ;;
        esac
    fi
    if command -v alive >/dev/null 2>&1; then
        for metadata in "$FIXTURE"/pids/*; do
            [ -f "$metadata" ] || continue
            read -r cleanup_pid cleanup_start < "$metadata" || continue
            [ "$cleanup_pid" != "$$" ] || continue
            if alive "$cleanup_pid" "$cleanup_start"; then
                kill -KILL "$cleanup_pid" 2>/dev/null || :
            fi
        done
    fi
    case "$FIXTURE" in /tmp/kindle-lock-fixture.*) rm -rf "$FIXTURE" ;; esac
    exit "$rc"
}
trap cleanup_fixture 0
trap 'exit 130' INT
trap 'exit 143' TERM HUP
