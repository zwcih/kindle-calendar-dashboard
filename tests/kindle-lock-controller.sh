#!/bin/sh
set -u
. "$FIXTURE/common.sh"
. "$FIXTURE/config.sh"
register
ROLE=${1-}
breadcrumb "controller-entry role=$ROLE"
trap 'fixture_exit=$?; breadcrumb "controller-exit status=$fixture_exit"' 0
critical=0 launching=0 pending_signal=0 child= child_start=
CHILD_FILE=$RUN/child
calendar_lock_require || exit "$?"
case "$ROLE" in --run|--guard|--touch) calendar_lock_inherited || exit "$?" ;; esac
cleanup() {
    fixture_exit=$?
    breadcrumb "controller-cleanup status=$fixture_exit"
    printf '%s\n' "$fixture_exit" > "$CASE/controller.exit"
}
record() { :; }
preflight() {
    . "$FIXTURE/preflight-lock-entry.sh" || return "$?"
    barrier before-guard
}
sleep() {
    # Keep actual guard timing; the startup's cosmetic delay is not under test.
    if [ "$ROLE" = --run ] && [ "$1" = 3 ]; then return 0; fi
    (exec 8>&- 9>&-; command sleep "$@")
}
cp() {
    if [ "${PHASE:-}" = before-copy ] && [ "$2" = "$RUN/controller.sh" ]; then
        barrier before-copy
    fi
    command cp "$@" || return "$?"
    if [ "${PHASE:-}" = after-copy ] && [ "$2" = "$RUN/config.local.conf" ]; then
        barrier after-copy
    fi
}
printf() {
    case "${PHASE:-}:$1" in
        after-spawn:Dedicated*)
            breadcrumb 'background-spawn-returned'
            barrier after-spawn
            ;;
    esac
    command printf "$@"
}
case "$ROLE" in
    --start)
        save_identity "launcher.$$"
        . "$FIXTURE/start-entry.sh"
        ;;
    --run)
        save_identity controller
        barrier before-owner
        . "$FIXTURE/owner-entry.sh"
        barrier controller
        ;;
    --guard)
        save_identity guardian
        # The actual guard owns FD 9 and performs its real owner-liveness loop.
        # Its device restoration is replaced by a barrier (tested separately).
        restore() { barrier guard-restore; }
        . "$FIXTURE/guard-entry.sh"
        ;;
esac
