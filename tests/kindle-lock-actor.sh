#!/bin/sh
set -u
. "$FIXTURE/common.sh"
register
action=$1
shift
case "$action" in
    suite-watchdog)
        read -r suite_pid suite_start < "$FIXTURE/suite.pid" || exit 1
        fixture_now
        suite_began=$fixture_seconds
        suite_ticks=0
        while alive "$suite_pid" "$suite_start"; do
            fixture_now
            if [ "$((fixture_seconds - suite_began))" -ge "$FIXTURE_SUITE_LIMIT" ] ||
                [ "$suite_ticks" -ge "$((FIXTURE_SUITE_LIMIT / 5))" ]; then
                printf 'SUITE_TIMEOUT limit=%s\n' "$FIXTURE_SUITE_LIMIT" >&2
                if alive "$suite_pid" "$suite_start"; then kill -TERM "$suite_pid"; fi
                exit 124
            fi
            (exec 8>&- 9>&-; command sleep 5)
            suite_ticks=$((suite_ticks + 1))
        done
        ;;
    fd-wait-comparison)
        calendar_lock_lifecycle || exit "$?"
        calendar_lock_resource || exit "$?"
        save_identity visibility
        : > "$CASE/visibility.ready"
        case "$1" in
            direct)
                # Deliberately reproduce the OLD fixture's redirection.
                command sleep 5 8>&- 9>&-
                ;;
            child)
                (exec 8>&- 9>&-; command sleep 5)
                ;;
            *) exit 99 ;;
        esac
        : > "$CASE/visibility.finished"
        wait_file "$CASE/visibility.go"
        ;;
    probe)
        calendar_lock_require || exit "$?"
        calendar_lock_legacy || exit "$?"
        calendar_lock_lifecycle || exit "$?"
        calendar_lock_resource
        ;;
    resource-probe) calendar_lock_resource ;;
    resource-only)
        calendar_lock_resource || exit "$?"
        save_identity resource-only
        barrier resource-only
        ;;
    hold)
        name=$1
        calendar_lock_lifecycle || exit "$?"
        calendar_lock_resource || exit "$?"
        save_identity "$name"
        barrier "$name"
        ;;
    contend)
        name=$1
        save_identity "$name"
        wait_file "$CASE/contenders.go"
        if calendar_lock_lifecycle && calendar_lock_resource; then
            : > "$CASE/$name.won"
            barrier "$name"
        else
            contender_rc=$?
            printf '%s\n' "$contender_rc" > "$CASE/$name.lost"
        fi
        ;;
    descendant)
        dedicated_trial=0
        TRIAL=$RUN
        . "$FIXTURE/worker-actual.sh"
        report() { :; }
        . "$FIXTURE/worker-entry.sh"
        save_identity worker
        /bin/sh "$FIXTURE/actor.sh" inherited-child &
        wait_file "$CASE/descendant.ready"
        barrier worker
        ;;
    run-parent)
        calendar_lock_lifecycle || exit "$?"
        calendar_lock_resource || exit "$?"
        ROLE=fixture CHILD_FILE=$RUN/child
        child= child_start= launching=0 pending_signal=0
        save_identity run-parent
        run 20 /bin/sh "$FIXTURE/actor.sh" inherited-child
        ;;
    worker-cleanup)
        dedicated_trial=0 TRIAL=$RUN
        . "$FIXTURE/worker-actual.sh"
        . "$FIXTURE/worker-cleanup.sh"
        . "$FIXTURE/worker-entry.sh"
        child_pid= child_start= pending_signal=0 radio_owned=0 wifi_owned=0 log_failed=0
        WORK=$CASE/workspace
        mkdir "$WORK" || exit 1
        # Model a completed command that left a descendant, not a tracked direct child.
        /bin/sh "$FIXTURE/actor.sh" inherited-child &
        wait_file "$CASE/descendant.ready"
        cleanup
        ;;
    inherited-child)
        calendar_lock_inherited || exit "$?"
        save_identity descendant
        barrier descendant
        ;;
    close-child)
        calendar_lock_inherited || exit "$?"
        exec 8>&-
        exec 9>&-
        : > "$CASE/closed"
        ;;
    parent-close-test)
        calendar_lock_lifecycle || exit "$?"
        calendar_lock_resource || exit "$?"
        /bin/sh "$FIXTURE/actor.sh" close-child || exit "$?"
        save_identity parent
        barrier parent
        ;;
    worker)
        dedicated_trial=$1
        TRIAL=$RUN
        . "$FIXTURE/worker-actual.sh"
        report() { :; }
        . "$FIXTURE/worker-entry.sh"
        save_identity authorized-worker
        barrier authorized-worker
        ;;
    authorized-parent)
        calendar_lock_lifecycle || exit "$?"
        save_identity authorized-parent
        cp "$CASE/authorized-parent.pid" "$RUN/owner" || exit 1
        : > "$RUN/ui-owned"
        : > "$RUN/sleep-owned"
        /bin/sh "$FIXTURE/actor.sh" worker 1 &
        wait "$!"
        ;;
    restore)
        # UI calls are the only mocks. The real restorer acquires/releases FD 8.
        run() { : > "$CASE/ui-call"; return 0; }
        job_state() { : > "$CASE/ui-call"; job_value='lab126_gui start/running'; }
        ui_ready() { return 0; }
        restore
        ;;
    restore-spawn)
        calendar_lock_lifecycle || exit "$?"
        run() {
            /bin/sh "$FIXTURE/actor.sh" resource-child &
            wait_file "$CASE/resource-child.ready"
        }
        restore || exit "$?"
        save_identity restorer
        barrier restorer
        ;;
    resource-child)
        calendar_lock_inherited || exit "$?"
        save_identity resource-child
        barrier resource-child
        ;;
    stop-signal)
        calendar_lock_lifecycle || exit "$?"
        printf 'DIAG fixture lifecycle=%s resolved=%s self_fd=%s resolved_fd=%s\n' \
            "$CALENDAR_LIFECYCLE_LOCK" "$(readlink -f "$CALENDAR_LIFECYCLE_LOCK")" \
            "$(readlink "/proc/$$/fd/9")" "$(readlink -f "/proc/$$/fd/9")"
        calendar_lock_inherited || exit "$?"
        ROLE=--run
        case "$1" in
            launching) critical=0 launching=1 pending_signal=0 expected_pending=143 ;;
            critical) critical=1 launching=0 pending_signal=0 expected_pending=0 ;;
            both) critical=1 launching=1 pending_signal=143 expected_pending=143 ;;
            *) exit 99 ;;
        esac
        trap request_stop USR1
        save_identity signal-owner
        cp "$CASE/signal-owner.pid" "$RUN/owner" || exit 1
        : > "$CASE/signal-owner.ready"
        wait_file "$RUN/stop-requested"
        [ "$pending_signal" = "$expected_pending" ] || fail 'stop deferral changed'
        barrier signal-observed
        critical=0
        if [ "$expected_pending" = 143 ]; then honor_signal
        else signal_exit 143; fi
        fail 'stop request was lost after leaving protected interval'
        ;;
    cleanup-signal)
        . "$FIXTURE/controller-cleanup.sh"
        ROLE=--run critical=0 launching=0 pending_signal=0 cleaning=0
        trap request_stop USR1
        stop_touch() {
            kill -USR1 "$$" || exit 92
            : > "$CASE/touch-cleanup-returned"
        }
        cancel_child() { : > "$CASE/child-cancelled"; }
        restore() {
            kill -USR1 "$$" || exit 93
            : > "$CASE/restore-returned"
        }
        log() {
            case "$*" in END:*) : > "$CASE/cleanup-ended" ;; esac
        }
        (exit 7)
        cleanup
        ;;
    start) exec /bin/sh "$FIXTURE/controller.sh" --start ;;
    stop)
        # Only device operations are mocked; entry, lifecycle and restore are real.
        run() { : > "$CASE/ui-call"; return 0; }
        job_state() { : > "$CASE/ui-call"; job_value='lab126_gui start/running'; }
        ui_ready() { return 0; }
        . "$FIXTURE/stop-entry.sh"
        ;;
    *) exit 99 ;;
esac
