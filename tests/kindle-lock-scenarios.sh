#!/bin/sh
new_case() {
    CASE=$FIXTURE/$1
    export CASE
    mkdir "$CASE" "$CASE/installed"
    . "$FIXTURE/common.sh"
    PHASE=
    export PHASE
    breadcrumb "case-start ${CASE##*/}"
}
probe() { /bin/sh "$FIXTURE/actor.sh" probe > "$CASE/probe.log" 2>&1; }
recover() { /bin/sh "$FIXTURE/actor.sh" stop --recover > "$CASE/recover.log" 2>&1; }
stop_request() { /bin/sh "$FIXTURE/actor.sh" stop --stop > "$CASE/stop.log" 2>&1; }
snapshot() {
    for snapshot_file in "$RUN"/*; do
        [ -f "$snapshot_file" ] || continue
        printf '%s\n' "${snapshot_file##*/}"
        cat "$snapshot_file"
    done
}
busy_unchanged() {
    breadcrumb 'busy-state-check-begin'
    before=$(snapshot)
    expect 11 recover
    expect 11 stop_request
    after=$(snapshot)
    [ "$before" = "$after" ] || fail 'busy recovery modified runtime'
    [ ! -f "$RUN/stop-requested" ] || fail 'busy launcher wrote stop marker'
    breadcrumb 'busy-state-check-passed'
}
install_fixture() {
    # Packaging uses real config loading and the actual --start copy sequence.
    cp "$FIXTURE/actor.sh" "$DIR/calendar-auto-refresh.sh"
    cp "$FIXTURE/config.sh" "$DIR/calendar-config.sh"
    cp "$FIXTURE/lock.sh" "$DIR/calendar-lock.sh"
    printf '%s\n' 'IMAGE_URL=https://calendar.invalid/image.png' 'WIFI_SSID=fixture-network' > "$DIR/config.local.conf"
}
start_busy() {
    breadcrumb 'contending-start-check-begin'
    before=$(snapshot)
    expect 32 /bin/sh "$FIXTURE/actor.sh" start > "$CASE/second-start.log" 2>&1
    after=$(snapshot)
    [ "$before" = "$after" ] || fail 'contending start modified runtime'
    breadcrumb 'contending-start-check-passed'
}
two_contenders() {
    releasing_pid=${1-}
    spawn first contend first
    first_pid=$spawned
    spawn second contend second
    second_pid=$spawned
    wait_file "$CASE/first.pid"
    wait_file "$CASE/second.pid"
    : > "$CASE/contenders.go"
    if [ -n "$releasing_pid" ]; then
        # Race normal owner close against both already-running contenders.
        : > "$CASE/owner.go"
        wait "$releasing_pid"
    fi
    outcome_ticks=0
    fixture_now
    outcome_started=$fixture_seconds
    while :; do
        if { [ -f "$CASE/first.won" ] || [ -f "$CASE/first.lost" ]; } &&
            { [ -f "$CASE/second.won" ] || [ -f "$CASE/second.lost" ]; }; then break; fi
        fixture_now
        [ "$outcome_ticks" -lt "$FIXTURE_WAIT_LIMIT" ] &&
            [ "$((fixture_seconds - outcome_started))" -lt "$FIXTURE_WAIT_LIMIT" ] ||
            fail "contender outcome timeout: limit=$FIXTURE_WAIT_LIMIT"
        sleep 1
        outcome_ticks=$((outcome_ticks + 1))
    done
    if [ -n "$releasing_pid" ] && [ -f "$CASE/first.lost" ] &&
        [ -f "$CASE/second.lost" ]; then
        # Both nonblocking attempts may linearize before the owner closes.
        for name in first second; do
            read -r lost_rc < "$CASE/$name.lost"
            [ "$lost_rc" = 32 ] || fail 'release racer did not fail closed'
            [ ! -f "$CASE/$name.won" ] || fail 'racer both won and lost'
        done
        wait "$first_pid"
        wait "$second_pid"
        expect 0 probe
        return
    fi
    if [ -f "$CASE/first.won" ]; then winner=first; loser=second
    else winner=second; loser=first; fi
    [ ! -f "$CASE/$loser.won" ] || fail 'two exclusive winners'
    [ -f "$CASE/$winner.won" ] || fail 'no acquisition after release'
    read -r lost_rc < "$CASE/$loser.lost"
    [ "$lost_rc" = 32 ] || fail 'loser did not fail closed'
    expect 32 probe
    : > "$CASE/$winner.go"
    wait "$first_pid"
    wait "$second_pid"
    expect 0 probe
    [ -f "$CALENDAR_LIFECYCLE_LOCK" ] && [ -f "$CALENDAR_RESOURCE_LOCK" ] ||
        fail 'release unlinked persistent lock inode'
}

for wait_mode in direct child; do
    new_case "fd-visibility-$wait_mode"
    spawn visibility fd-wait-comparison "$wait_mode"
    visibility_pid=$spawned
    wait_file "$CASE/visibility.ready"
    fd_visible=0
    fd_missing=0
    for sample in 1 2 3; do
        command sleep 1
        [ ! -f "$CASE/visibility.finished" ] || fail 'visibility sampling missed controlled sleep'
        if calendar_lock_has_lifecycle_fd "$visibility_pid"; then
            fd_visible=$((fd_visible + 1))
        else
            fd_missing=$((fd_missing + 1))
        fi
        expect 32 probe
        diagnose_fixture_pid "$CASE/visibility.pid"
    done
    printf 'FD_VISIBILITY mode=%s visible=%s missing=%s lock_busy_samples=3\n' \
        "$wait_mode" "$fd_visible" "$fd_missing"
    # Direct external-command redirection differs between shells; report it.
    # The corrected child-only form MUST preserve the holder's FD 9.
    if [ "$wait_mode" = child ]; then
        [ "$fd_visible" = 3 ] && [ "$fd_missing" = 0 ] ||
            fail 'explicit child wait moved the parent lifecycle FD'
    fi
    : > "$CASE/visibility.go"
    wait "$visibility_pid"
    expect 0 probe
done
printf 'PASS direct versus child-only sleep visibility comparison completed\n'

new_case dependency
(
    command() {
        if [ "$1" = -v ] && [ "$2" = flock ]; then return 1; fi
        command "$@"
    }
    expect 10 calendar_lock_require
)
(
    flock() { return 2; }
    expect 10 calendar_lock_lifecycle
    expect 10 calendar_lock_resource
)
[ ! -e "$RUN" ] || fail 'dependency failure created runtime'
printf 'PASS missing and unusable flock fail closed\n'

new_case legacy
mkdir "$CALENDAR_LEGACY_LOCK"
printf 'synthetic-old-owner\n' > "$CALENDAR_LEGACY_LOCK/owner"
expect 32 probe
expect 32 calendar_lock_resource
install_fixture
expect 32 /bin/sh "$FIXTURE/actor.sh" start > "$CASE/start.log" 2>&1
[ ! -e "$RUN" ] || fail 'legacy start changed runtime'
read -r legacy_owner < "$CALENDAR_LEGACY_LOCK/owner"
[ "$legacy_owner" = synthetic-old-owner ] || fail 'legacy metadata changed'
[ ! -e "$CALENDAR_LIFECYCLE_LOCK" ] || fail 'legacy check occurred after lifecycle acquisition'
printf 'PASS stale old-style lock is retained and refused\n'

new_case identity
identity "$$" || fail 'cannot read actual process identity'
current_start=$proc_start
expect 0 alive "$$" "$current_start"
expect 1 alive "$$" "$((current_start + 1))"
expect 1 alive invalid "$current_start"
printf 'PASS real live owner identity and synthetic PID-reuse start mismatch\n'

new_case unresolved-identity
(
    readlink() { return 1; }
    expect 1 calendar_lock_has_lifecycle_fd "$$"
    expect 32 calendar_lock_inherited
)
[ ! -e "$RUN" ] || fail 'unresolved identity modified runtime'
printf 'PASS unresolved lifecycle identity fails closed\n'

for signal_phase in launching critical both alias-launching; do
    new_case "signal-$signal_phase"
    if [ "$signal_phase" = alias-launching ]; then
        ln -s "$CASE" "$CASE.alias"
        CASE=$CASE.alias
        export CASE
        . "$FIXTURE/common.sh"
        signal_phase=launching
    fi
    mkdir "$RUN"
    spawn signal-owner stop-signal "$signal_phase"
    signal_pid=$spawned
    wait_file "$CASE/signal-owner.ready"
    read -r diagnostic_pid diagnostic_start < "$RUN/owner"
    printf 'DIAG fixture lifecycle=%s resolved=%s owner_fd=%s\n' \
        "$CALENDAR_LIFECYCLE_LOCK" "$(readlink -f "$CALENDAR_LIFECYCLE_LOCK")" \
        "$(readlink "/proc/$diagnostic_pid/fd/9")"
    expect 0 recover
    wait_file "$CASE/signal-observed.ready"
    [ -f "$RUN/stop-requested" ] || fail 'owner lost stop marker'
    expect 32 probe
    : > "$CASE/signal-observed.go"
    wait "$signal_pid"
    expect 0 probe
    printf 'PASS real USR1 stop preserves marker and deferred status during %s\n' "$signal_phase"
done

new_case cleanup-signal
mkdir "$RUN"
expect 7 /bin/sh "$FIXTURE/actor.sh" cleanup-signal > "$CASE/cleanup.log" 2>&1
for marker in touch-cleanup-returned child-cancelled restore-returned cleanup-ended; do
    [ -f "$CASE/$marker" ] || fail "USR1 interrupted cleanup: $marker"
done
printf 'PASS real repeated USR1 cannot interrupt controller cleanup or restoration\n'

for release in killed normal; do
    new_case "release-$release"
    spawn owner hold owner
    owner_pid=$spawned
    wait_file "$CASE/owner.ready"
    expect 32 probe
    # Keep another FD open without locking it, then compare /proc inode identities.
    exec 7>>"$CALENDAR_LIFECYCLE_LOCK"
    inode_before=$(readlink "/proc/$$/fd/7")
    if [ "$release" = killed ]; then
        kill_exact "$CASE/owner.spawn" "$owner_pid"
        wait_killed "$owner_pid"
        two_contenders
    else
        two_contenders "$owner_pid"
    fi
    inode_after=$(readlink "/proc/$$/fd/7")
    [ "$inode_before" = "$inode_after" ] || fail 'lock inode was removed'
    case "$inode_after" in *' (deleted)') fail 'deleted lock inode' ;; esac
    exec 7>&-
    printf 'PASS %s holder release versus two real concurrent acquisitions\n' "$release"
done

new_case close-child
spawn parent parent-close-test
parent_pid=$spawned
wait_file "$CASE/parent.ready"
wait_file "$CASE/closed"
expect 32 probe
expect 32 /bin/sh "$FIXTURE/actor.sh" resource-probe > "$CASE/resource.log" 2>&1
: > "$CASE/parent.go"
wait "$parent_pid"
expect 0 probe
printf 'PASS child closes only its FDs without unlocking the parent\n'

new_case descendant
mkdir "$RUN"
: > "$RUN/ui-owned"
spawn worker descendant
worker_pid=$spawned
wait_file "$CASE/worker.ready"
kill_exact "$CASE/worker.spawn" "$worker_pid"
wait_killed "$worker_pid"
expect 32 probe
expect 1 /bin/sh "$FIXTURE/actor.sh" restore > "$CASE/restore-busy.log" 2>&1
[ -f "$RUN/ui-owned" ] && [ ! -f "$CASE/ui-call" ] && [ ! -f "$RUN/restored" ] ||
    fail 'resource-busy restoration touched UI'
busy_unchanged
: > "$CASE/descendant.go"
await_dead "$CASE/descendant.pid"
expect 0 probe
expect 0 /bin/sh "$FIXTURE/actor.sh" restore
[ -f "$RUN/restored" ] && [ -f "$CASE/ui-call" ] && [ ! -f "$RUN/ui-owned" ] ||
    fail 'restoration did not resume after descendant release'
printf 'PASS SIGKILL descendant retention and restoration refusing live resource\n'

new_case run-descendant
mkdir "$RUN"
spawn run-parent run-parent
run_pid=$spawned
wait_file "$CASE/descendant.ready"
wait_file "$RUN/child"
kill_exact "$CASE/run-parent.spawn" "$run_pid"
wait_killed "$run_pid"
expect 32 probe
: > "$CASE/descendant.go"
await_dead "$CASE/descendant.pid"
expect 0 probe
printf 'PASS actual run() command retains locks after controller SIGKILL\n'

new_case worker-cleanup
spawn cleanup worker-cleanup
cleanup_pid=$spawned
wait_file "$CASE/descendant.ready"
exec 7>>"$CALENDAR_RESOURCE_LOCK"
resource_inode=$(readlink "/proc/$$/fd/7")
wait "$cleanup_pid"
[ ! -d "$CASE/workspace" ] || fail 'worker cleanup did not remove owned workspace'
[ "$(readlink "/proc/$$/fd/7")" = "$resource_inode" ] || fail 'worker cleanup removed resource inode'
expect 32 probe
expect 32 /bin/sh "$FIXTURE/actor.sh" resource-probe > "$CASE/resource-busy.log" 2>&1
: > "$CASE/descendant.go"
await_dead "$CASE/descendant.pid"
expect 0 probe
exec 7>&-
printf 'PASS actual worker cleanup preserves descendant locks and permanent resource inode\n'

new_case restore-child
mkdir "$RUN"
spawn restorer restore-spawn
restorer_pid=$spawned
wait_file "$CASE/restorer.ready"
[ -f "$RUN/restored" ] || fail 'actual restore did not complete'
expect 32 /bin/sh "$FIXTURE/actor.sh" resource-probe > "$CASE/resource-busy.log" 2>&1
: > "$CASE/resource-child.go"
await_dead "$CASE/resource-child.pid"
expect 0 /bin/sh "$FIXTURE/actor.sh" resource-probe
expect 32 probe
: > "$CASE/restorer.go"
wait "$restorer_pid"
expect 0 probe
printf 'PASS actual restore closes only its FD, retaining its command child lock\n'

new_case worker-auth
mkdir "$RUN"
spawn authorized authorized-parent
authorized_pid=$spawned
wait_file "$CASE/authorized-worker.ready"
expect 32 probe
expect 32 /bin/sh "$FIXTURE/actor.sh" worker 1 > "$CASE/unauthorized.log" 2>&1
: > "$CASE/authorized-worker.go"
wait "$authorized_pid"
expect 0 probe
# Use actual identity(), not an identity mock, for the direct-parent stale-start case.
. "$FIXTURE/worker-actual.sh"
TRIAL=$RUN
identity "$PPID" || fail 'parent identity unavailable'
printf '%s %s\n' "$PPID" "$((proc_start + 1))" > "$RUN/owner"
expect 1 dedicated_owner_alive
. "$FIXTURE/actual.sh"
printf 'PASS dedicated worker direct-parent authorization and inherited lock\n'

for initial in first restored; do
    for phase in before-copy after-copy after-spawn; do
        new_case "start-$initial-$phase"
        install_fixture
        if [ "$initial" = restored ]; then
            mkdir "$RUN"
            : > "$RUN/restored"
            printf 'retained synthetic capture\n' > "$RUN/output.synthetic"
        fi
        PHASE=$phase
        export PHASE
        spawn launcher start
        launcher_pid=$spawned
        wait_file "$CASE/$phase.ready"
        breadcrumb "launcher-at-controlled-phase $phase"
        if [ "$phase" = after-spawn ]; then
            # Prove the background controller runs while the launcher is paused.
            wait_file "$CASE/before-owner.ready"
            read -r pinned_controller pinned_controller_start < "$CASE/controller.pid"
            breadcrumb 'controller-before-owner-while-launcher-paused'
        fi
        start_busy
        busy_unchanged
        [ ! -f "$RUN/owner" ] || fail 'owner published before controlled barrier'
        if [ "$phase" = before-copy ]; then
            [ ! -f "$RUN/controller.sh" ] || fail 'copy occurred before copy barrier'
        else
            for copied in controller.sh refresh.sh calendar-config.sh calendar-lock.sh config.local.conf; do
                [ -f "$RUN/$copied" ] || fail "missing packaged file $copied"
            done
        fi
        if [ "$initial" = restored ]; then
            read -r retained < "$RUN/output.synthetic"
            [ "$retained" = 'retained synthetic capture' ] || fail 'unowned capture changed'
        fi
        breadcrumb "killing-launcher phase=$phase"
        read -r pinned_launcher pinned_launcher_start < "$CASE/launcher.spawn"
        [ "$pinned_launcher" = "$launcher_pid" ] || fail 'contender replaced initial launcher identity'
        kill_exact "$CASE/launcher.spawn" "$launcher_pid"
        wait_killed "$launcher_pid"
        if [ "$phase" = after-spawn ]; then
            breadcrumb 'launcher-dead-checking-controller-held-lock'
            # Launcher death does not release the asynchronously inherited FD 9.
            start_busy
            busy_unchanged
            expect 32 probe
            kill_exact "$CASE/controller.pid" "$pinned_controller"
        fi
        expect 0 probe
        expect 20 /bin/sh "$FIXTURE/actor.sh" start > "$CASE/unrestored-start.log" 2>&1
        expect 0 recover
        [ -f "$RUN/restored" ] && [ -f "$CASE/ui-call" ] ||
            fail 'actual recovery entry did not recover interrupted startup'
        printf 'PASS %s start concurrency and launcher SIGKILL %s\n' "$initial" "$phase"
    done
done

new_case preflight-busy
install_fixture
spawn resource-only resource-only
resource_pid=$spawned
wait_file "$CASE/resource-only.ready"
spawn launcher start
launcher_pid=$spawned
wait_file "$CASE/before-owner.ready"
wait "$launcher_pid"
: > "$CASE/before-owner.go"
await_dead "$CASE/controller.pid"
[ -f "$RUN/owner" ] || fail 'controller did not reach actual preflight'
read -r preflight_rc < "$CASE/controller.exit"
[ "$preflight_rc" = 32 ] || fail 'busy preflight returned unexpected status'
[ ! -f "$CASE/before-guard.ready" ] && [ ! -f "$RUN/guard" ] &&
    [ ! -f "$RUN/guard-ready" ] && [ ! -f "$RUN/ui-owned" ] ||
    fail 'busy preflight proceeded to guard or UI ownership'
expect 32 /bin/sh "$FIXTURE/actor.sh" resource-probe > "$CASE/resource-busy.log" 2>&1
: > "$CASE/resource-only.go"
wait "$resource_pid"
expect 0 probe
printf 'PASS actual busy resource preflight refuses before guardian spawn\n'

new_case owner-guard
install_fixture
spawn launcher start
launcher_pid=$spawned
wait_file "$CASE/before-owner.ready"
wait "$launcher_pid"
start_busy
busy_unchanged
# A live process with a matching start time is still not an owner without FD 9.
identity "$$" || fail 'fixture identity unavailable'
printf '%s %s\n' "$$" "$proc_start" > "$RUN/owner"
busy_unchanged
rm -f "$RUN/owner"
: > "$CASE/before-owner.go"
wait_file "$CASE/before-guard.ready"
[ -f "$RUN/owner" ] && [ ! -f "$RUN/guard" ] || fail 'owner/guard publication ordering'
expect 0 /bin/sh "$FIXTURE/actor.sh" resource-probe
expect 32 probe
start_busy
cp "$RUN/owner" "$CASE/real-owner"
read -r owner_id owner_start < "$RUN/owner"
printf '%s %s\n' "$owner_id" "$((owner_start + 1))" > "$RUN/owner"
busy_unchanged
cp "$CASE/real-owner" "$RUN/owner"
: > "$CASE/before-guard.go"
wait_file "$CASE/controller.ready"
wait_file "$RUN/guard-ready"
expect 32 probe
expect 0 /bin/sh "$FIXTURE/actor.sh" resource-probe
# Stop requester can only signal the live exact owner; its actual trap writes the marker.
expect 0 recover
wait_file "$RUN/stop-requested"
await_dead "$CASE/controller.pid"
wait_file "$CASE/guard-restore.ready"
start_busy
# With the owner gone, even published stale metadata cannot authorize a busy write.
before=$(snapshot)
expect 11 recover
[ "$before" = "$(snapshot)" ] || fail 'busy guard recovery changed state'
expect 32 probe
: > "$CASE/guard-restore.go"
await_dead "$CASE/guardian.pid"
expect 0 probe
printf 'PASS owner USR1 trap, PID reuse rejection and independent guard inherited lifetime\n'

new_case owner-before-guard-death
install_fixture
spawn launcher start
launcher_pid=$spawned
wait_file "$CASE/before-owner.ready"
wait "$launcher_pid"
: > "$CASE/before-owner.go"
wait_file "$CASE/before-guard.ready"
start_busy
read -r pinned_controller pinned_controller_start < "$CASE/controller.pid"
kill_exact "$CASE/controller.pid" "$pinned_controller"
[ ! -f "$RUN/guard" ] || fail 'guard unexpectedly launched'
expect 0 probe
printf 'PASS controller SIGKILL after owner publication and before guard spawn\n'
printf 'PASS all real Linux lock regressions\n'
