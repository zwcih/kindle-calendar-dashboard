#!/bin/sh

PATH=/usr/bin:/bin:/usr/sbin:/sbin
TZ=CST-8
export PATH TZ
umask 077
set -f
DIR=/mnt/us/kindle-dashboard
RUN=/tmp/calendar-dedicated
LOG=$DIR/dedicated-schedule.log
RTC=/sys/devices/platform/imx-i2c.0/i2c-0/0-003c/max77696-rtc.0
child=
child_start=
critical=0
launching=0
pending_signal=0
cleaning=0
ROLE=${1-}
CHILD_FILE=$RUN/child
if [ "${1-}" = --touch ]; then CHILD_FILE=$RUN/touch-child; fi

identity() {
    [ -r "/proc/$1/stat" ] || return 1
    IFS= read -r stat_line < "/proc/$1/stat" || return 1
    fields=${stat_line##*) }
    [ "$fields" != "$stat_line" ] || return 1
    set -- $fields
    [ "$#" -ge 20 ] || return 1
    proc_state=$1
    shift 19
    proc_start=$1
    case "$proc_start" in ''|*[!0-9]*) return 1 ;; esac
}
alive() {
    case "$1:$2" in *[!0-9:]*|:*|*:) return 1 ;; esac
    identity "$1" && [ "$proc_start" = "$2" ] &&
        [ "$proc_state" != Z ] && [ "$proc_state" != X ]
}
log() {
    [ ! -L "$LOG" ] && { [ ! -e "$LOG" ] || [ -f "$LOG" ]; } || return 1
    log_tmp=$(mktemp "$DIR/.dedicated-log.XXXXXX") || return 1
    if [ -f "$LOG" ]; then
        tail -c 57344 "$LOG" > "$log_tmp" || { rm -f "$log_tmp"; return 1; }
    fi
    if ! printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$*" >> "$log_tmp" ||
        ! mv -f "$log_tmp" "$LOG"; then
        rm -f "$log_tmp"
        return 1
    fi
}
record() { log "$*" || { printf 'Dedicated log write failed.\n' >&2; exit 14; }; }

lease() {
    [ "${1-}" -ge 1 ] && [ "$1" -le 43320 ] || return 1
    lease_now=$(date +%s) || return 1
    printf '%s\n' "$((lease_now + $1))" > "$RUN/deadline.$$" &&
        mv -f "$RUN/deadline.$$" "$RUN/deadline"
}
guard_ok() {
    [ "$ROLE" = --run ] && [ -f "$RUN/sleep-owned" ] && [ ! -f "$RUN/stopping" ] || return 0
    read -r guard_pid guard_start < "$RUN/guard" && alive "$guard_pid" "$guard_start"
}
signal_exit() {
    [ "$critical" = 0 ] || return 0
    if [ "$launching" = 1 ]; then pending_signal=$1; return 0; fi
    if [ "$ROLE" = --run ] && [ -f "$RUN/stop-requested" ]; then exit 0; fi
    exit "$1"
}
honor_signal() {
    launching=0
    [ "$pending_signal" = 0 ] || signal_exit "$pending_signal"
}
request_stop() {
    [ "${cleaning:-0}" = 0 ] || return 0
    # The live controller writes its own marker while holding FD 9. A --stop
    # launcher must not write through a runtime-directory replacement race.
    : > "$RUN/stop-requested" || exit 14
    signal_exit 143
}

cancel_child() {
    if [ -n "$child" ]; then
        if alive "$child" "$child_start"; then
            kill -TERM "$child" 2>/dev/null
            grace=0
            while alive "$child" "$child_start" && [ "$grace" -lt 25 ]; do
                sleep 1
                grace=$((grace + 1))
            done
            if alive "$child" "$child_start"; then kill -KILL "$child" 2>/dev/null; fi
        fi
        wait "$child" 2>/dev/null
    fi
    child=
    child_start=
    rm -f "$CHILD_FILE"
}
run() {
    limit=$1
    shift
    guard_ok || { log 'GUARD_LOST: returning to the reading interface.'; return 16; }
    lease "$((limit + 90))" || return 14
    : > "$RUN/output.$$" || return 14
    launching=1
    "$@" > "$RUN/output.$$" 2>&1 &
    child=$!
    child_start=
    if identity "$child"; then child_start=$proc_start; fi
    if ! printf '%s %s\n' "$child" "$child_start" > "$CHILD_FILE"; then
        honor_signal
        cancel_child
        return 14
    fi
    honor_signal
    began=$(date +%s) || { cancel_child; return 12; }
    ticks=0
    while alive "$child" "$child_start"; do
        if ! guard_ok; then
            cancel_child
            log 'GUARD_LOST: command cancelled; restoring UI.'
            return 16
        fi
        now=$(date +%s) || { cancel_child; return 12; }
        if [ "$((now - began))" -ge "$limit" ] || [ "$ticks" -ge "$limit" ]; then
            cancel_child
            return 124
        fi
        sleep 1 || { cancel_child; return 12; }
        ticks=$((ticks + 1))
    done
    wait "$child"
    result=$?
    child=
    child_start=
    rm -f "$CHILD_FILE" || return 14
    return "$result"
}
must() {
    label=$1
    shift
    if run "$@"; then
        record "$label: exit=0"
    else
        rc=$?
        detail=$(tail -c 2048 "$RUN/output.$$")
        record "$label: exit=$rc $detail"
        exit "$rc"
    fi
}
property() {
    run 4 lipc-get-prop "$@" || return "$?"
    value=$(cat "$RUN/output.$$") || return 14
}
job_state() {
    run 4 initctl status "$1" || return "$?"
    job_value=$(cat "$RUN/output.$$") || return 14
}
ui_ready() {
    for job in lab126_gui framework pillow webreader; do
        job_state "$job" || return 1
        case "$job_value" in "$job start/running"*) ;; *) return 1 ;; esac
    done
}
read_number() {
    IFS= read -r number < "$1" || return 1
    case "$number" in ''|*[!0-9]*) return 1 ;; esac
}
stop_touch() {
    rm -f "$RUN/touch-required"
    if [ -r "$RUN/touch" ] && read -r touch_pid touch_start < "$RUN/touch" &&
        alive "$touch_pid" "$touch_start"; then
        kill -TERM "$touch_pid" 2>/dev/null
        touch_wait=0
        while alive "$touch_pid" "$touch_start" && [ "$touch_wait" -lt 5 ]; do
            sleep 1
            touch_wait=$((touch_wait + 1))
        done
        if alive "$touch_pid" "$touch_start"; then kill -KILL "$touch_pid" 2>/dev/null; fi
    fi
    if [ -r "$RUN/touch-child" ]; then
        read -r touch_child touch_child_start < "$RUN/touch-child"
        if alive "$touch_child" "$touch_child_start"; then
            kill -TERM "$touch_child" 2>/dev/null
        fi
    fi
}
restore() {
    # Independent OFDs also serialize controller and guard, which share FD 9.
    # A killed worker's surviving fbink/curl must finish before UI restoration.
    if ! calendar_lock_resource; then
        log 'RECOVERY_BUSY: resource still held or lock unverifiable; state retained, retry recovery after commands finish.'
        return 1
    fi
    restore_failed=0
    if [ -f "$RUN/ui-owned" ]; then
        if job_state lab126_gui; then
            case "$job_value" in
                "lab126_gui start/running"*) ;;
                *)
                    if ! run 45 initctl start lab126_gui; then
                        log 'RECOVERY_ERROR: lab126_gui start failed or timed out.'
                        restore_failed=1
                    fi
                    ;;
            esac
        else
            log 'RECOVERY_ERROR: lab126_gui status unavailable.'
            restore_failed=1
        fi
        ready=0
        for attempt in 1 2 3 4 5 6 7 8; do
            if ui_ready; then ready=1; break; fi
            sleep 1
        done
        if [ "$ready" = 1 ]; then
            rm -f "$RUN/ui-owned" || restore_failed=1
            log 'GUI_RESTORED: lab126_gui/framework/pillow/webreader running.'
        else
            log 'RECOVERY_ERROR: GUI services did not become ready.'
            restore_failed=1
        fi
    fi
    if [ -f "$RUN/light-owned" ]; then
        if read_number "$RUN/old-light" &&
            run 4 lipc-set-prop -i com.lab126.powerd flIntensity "$number" &&
            property -i com.lab126.powerd flIntensity && [ "$value" = "$number" ]; then
            rm -f "$RUN/light-owned" || restore_failed=1
        else
            log 'RECOVERY_ERROR: frontlight restoration failed.'
            restore_failed=1
        fi
    fi
    if [ -f "$RUN/sleep-owned" ]; then
        if read_number "$RUN/old-sleep" && [ "$number" = 0 ] &&
            run 4 lipc-set-prop -i com.lab126.powerd preventScreenSaver "$number" &&
            property -i com.lab126.powerd preventScreenSaver && [ "$value" = "$number" ]; then
            rm -f "$RUN/sleep-owned" || restore_failed=1
        else
            log 'RECOVERY_ERROR: native sleep restoration failed.'
            restore_failed=1
        fi
    fi
    if [ "$restore_failed" = 0 ]; then
        if run 8 lipc-set-prop com.lab126.appmgrd start app://com.lab126.booklet.home; then
            : > "$RUN/restored" || restore_failed=1
            log 'RESTORED: Home requested; original frontlight and native sleep restored.'
        else
            log 'RECOVERY_ERROR: Home request failed.'
            restore_failed=1
        fi
    fi
    exec 8>&-
    if [ "$restore_failed" = 0 ]; then rm -f "$RUN/output.$$" || return 1; fi
    [ "$restore_failed" = 0 ]
}
cleanup() {
    final=$? cleaning=1
    trap '' HUP INT TERM USR1
    trap - 0
    : > "$RUN/stopping"
    if [ -f "$RUN/stop-requested" ]; then log 'EXIT_REQUESTED: long press or explicit stop; restoring reading interface.'; fi
    stop_touch
    cancel_child
    if ! restore; then final=20; fi
    log "END: exit=$final; runtime retained for recovery; no boot hooks. A previously submitted alarm may still wake once."
    exit "$final"
}
preflight() {
    # FD 9 prevents new independent workers after this resource availability
    # check. Close FD 8 before spawning the guardian (an independent restorer).
    calendar_lock_resource || return "$?"
    exec 8>&-
    for other in /tmp/calendar-legacy-auto /tmp/calendar-sleep-wake-test \
        /tmp/calendar-network-diagnostic.lock /tmp/calendar-legacy-trial.lock; do
        [ ! -e "$other" ] || { record "BLOCKED: another calendar task exists: $other"; return 11; }
    done
    if [ -r /tmp/calendar-dedicated-trial/owner ] &&
        read -r old_pid old_start < /tmp/calendar-dedicated-trial/owner &&
        alive "$old_pid" "$old_start"; then
        record 'BLOCKED: old dedicated prototype is still running.'
        return 11
    fi
    [ "$(cat /sys/class/rtc/rtc0/name)" = max77696-rtc.0 ] &&
        [ -w "$RTC/rtc_delta_alarm" ] && [ -w /sys/power/state ] ||
        { record 'BLOCKED: expected PW3 RTC/suspend interface unavailable.'; return 10; }
    run 4 kdb get system/daemon/powerd/SYS_RTC_WAKEUP || return 10
    [ "$(cat "$RUN/output.$$")" = "$RTC/rtc_delta_alarm" ] ||
        { record 'BLOCKED: RTC configuration differs from inspected device.'; return 10; }
    ui_ready || { record 'BLOCKED: expected GUI services are not all running.'; return 11; }
    property com.lab126.powerd state && [ "$value" = active ] ||
        { record 'BLOCKED: start while manually awake.'; return 11; }
    property -i com.lab126.powerd preventScreenSaver && [ "$value" = 0 ] ||
        { record 'BLOCKED: sleep prevention already enabled.'; return 11; }
    printf '%s\n' "$value" > "$RUN/old-sleep" || return 14
    property -i com.lab126.powerd flIntensity || return 12
    case "$value" in ''|*[!0-9]*) return 12 ;; esac
    printf '%s\n' "$value" > "$RUN/old-light" || return 14
    property -i com.lab126.cmd wirelessEnable && [ "$value" = 1 ] &&
        property -i com.lab126.wifid enable && [ "$value" = 1 ] &&
        property com.lab126.wifid cmState && [ "$value" = CONNECTED ] ||
        { record 'BLOCKED: connect the saved Wi-Fi network before starting dedicated mode.'; return 11; }
    property -i com.lab126.powerd battLevel || return 12
    case "$value" in ''|*[!0-9]*) return 12 ;; esac
    [ "$value" -ge 0 ] && [ "$value" -le 100 ] ||
        { record 'BLOCKED: invalid battery level.'; return 11; }
    [ "$(cat /sys/class/input/event1/device/name)" = cyttsp4_mt ] &&
        [ -c /dev/input/event1 ] && [ -r /dev/input/event1 ] ||
        { record 'BLOCKED: verified touchscreen exit interface unavailable.'; return 11; }
    [ -s "$DIR/dashboard.png" ] && [ -x /mnt/us/libkh/bin/fbink ] ||
        { record 'BLOCKED: cached image or FBInk unavailable.'; return 10; }
}
sleep_once() {
    duration=$1
    minimum=$2
    property com.lab126.powerd state && [ "$value" = active ] ||
        { record 'SUSPEND_ABORT: powerd no longer active.'; return 32; }
    IFS=' ' read -r up idle last before_total < /proc/uptime || return 12
    case "$before_total" in ''|*[!0-9]*) record 'SUSPEND_ABORT: kernel suspend counter unavailable.'; return 12 ;; esac
    read_number /sys/class/rtc/rtc0/since_epoch || return 12
    rtc_before=$number
    must RTC_SET 4 /bin/sh -c 'printf "%s\n" "$1" > "$2"' sh "$duration" "$RTC/rtc_delta_alarm"
    IFS=' ' read -r alarm rest < "$RTC/rtc_alarm0" || return 12
    case "$alarm" in ''|*[!0-9]*) return 12 ;; esac
    remaining=$((alarm - rtc_before))
    [ "$remaining" -ge "$duration" ] && [ "$remaining" -le "$((duration + 5))" ] ||
        { record "SUSPEND_ABORT: physical alarm read-back mismatch: delta=$remaining requested=$duration"; return 12; }
    run 4 cat "$RTC/rtc_irq" &&
        awk '/^RTC Alarm 1 IRQ/ && $NF == "enabled" { enabled=1 }
            END { exit !enabled }' "$RUN/output.$$" ||
        { record 'SUSPEND_ABORT: physical alarm interrupt is not enabled.'; return 12; }
    record "SUSPEND_BEGIN: requested=$duration physical_alarm=$alarm total_suspended_before=$before_total; powerd kept active."
    if [ -f "$RUN/manual-refresh" ] || [ -f "$RUN/gesture-held" ]; then
        record 'SUSPEND_DEFERRED: touch gesture or manual refresh pending.'
        return 34
    fi
    if ! run "$((duration + 30))" /bin/sh -c 'printf "mem\n" > /sys/power/state'; then
        record 'SUSPEND_FAILED: kernel suspend returned an error or exceeded deadline.'
        return 12
    fi
    IFS=' ' read -r up idle last after_total < /proc/uptime || return 12
    case "$after_total" in ''|*[!0-9]*) return 12 ;; esac
    slept=$((after_total - before_total))
    record "SUSPEND_END: actual_suspended_seconds=$slept last_suspend=$last total_suspended_after=$after_total"
    [ "$slept" -ge "$minimum" ] ||
        { record 'NO_SLEEP: kernel did not record enough suspended time.'; return 33; }
}

next_slot() {
    slot_now=$1
    local_epoch=$((slot_now + 28800))
    day=$((local_epoch / 86400))
    seconds=$((local_epoch % 86400))
    if [ "$seconds" -lt 23400 ]; then
        next=$((day * 86400 + 23400 - 28800))
    elif [ "$seconds" -ge 79200 ]; then
        next=$(((day + 1) * 86400 + 23400 - 28800))
    else
        next=$((day * 86400 + (seconds / 1800 + 1) * 1800 - 28800))
    fi
}
gesture_finish() {
    pressed=0
    source=
    rm -f "$RUN/gesture-held" || exit 14
    if [ "$multiple" = 1 ]; then
        multiple=0
        record 'GESTURE_IGNORED: use one finger.'
        return
    fi
    elapsed_sec=$((event_sec - down_sec))
    elapsed_usec=$((event_usec - down_usec))
    if [ "$elapsed_sec" -lt 0 ] || { [ "$elapsed_sec" = 0 ] && [ "$elapsed_usec" -lt 0 ]; }; then
        record 'GESTURE_IGNORED: input timestamp moved backwards.'
        return
    fi
    # Subtract seconds first, avoiding epoch-to-microsecond integer overflow.
    if [ "$elapsed_sec" -ge 3 ] ||
        [ "$((elapsed_sec * 1000000 + elapsed_usec))" -ge 2000000 ]; then
        : > "$RUN/stop-requested" || exit 14
        if alive "$touch_owner" "$touch_owner_start"; then
            kill -TERM "$touch_owner" || exit 12
        fi
        exit 0
    fi
    : > "$RUN/manual-refresh" || exit 14
}
gesture_event() {
    [ "$#" = 8 ] || exit 16
    for word do
        case "$word" in ''|*[!0-9]*|0[0-9]*) exit 16 ;; esac
        [ "${#word}" -le 5 ] && [ "$word" -le 65535 ] || exit 16
    done
    event_sec=$(($1 + $2 * 65536))
    event_usec=$(($3 + $4 * 65536))
    [ "$event_usec" -lt 1000000 ] || exit 16
    if [ -n "${last_sec-}" ] && { [ "$event_sec" -lt "$last_sec" ] ||
        { [ "$event_sec" = "$last_sec" ] && [ "$event_usec" -lt "$last_usec" ]; }; }; then
        # Suppress the entire contact epoch, not just the final release.
        [ "$pressed" = 0 ] || multiple=1
    fi
    last_sec=$event_sec
    last_usec=$event_usec
    if [ "$5" = 0 ] && [ "$6" = 3 ]; then
        record 'TOUCH_INPUT_DROPPED: contact state unreliable; restoring UI through guard.'
        exit 16
    fi
    if [ "$5" = 3 ] && [ "$6" = 47 ] && [ "$8" = 0 ]; then
        current_slot=$7
    elif [ "$5" = 3 ] && [ "$6" = 57 ]; then
        mt_seen=1
        remaining=
        previous=
        for contact in ${contacts-}; do
            if [ "${contact%%:*}" = "$current_slot" ]; then
                previous=${contact#*:}
            else
                remaining="$remaining $contact"
            fi
        done
        if [ "$8" -lt 32768 ]; then
            tracking_id=$7.$8
            if [ "$previous" = "$tracking_id" ]; then return; fi
            for contact in $remaining; do
                [ "${contact#*:}" != "$tracking_id" ] || {
                    record 'TOUCH_INPUT_INVALID: duplicate tracking identity; restoring UI.'
                    exit 16
                }
            done
            if [ "$pressed" = 0 ]; then
                pressed=1
                multiple=0
                down_sec=$event_sec
                down_usec=$event_usec
                : > "$RUN/gesture-held" || exit 14
            elif [ -n "$remaining" ] || [ -n "$previous" ]; then
                # Latch until ALL slots release, including replacement contacts.
                multiple=1
            fi
            contacts="$remaining $current_slot:$tracking_id"
            source=mt
        elif [ "$7" = 65535 ] && [ "$8" = 65535 ]; then
            contacts=$remaining
            if [ -n "$previous" ] && [ -z "$contacts" ]; then gesture_finish; fi
        fi
    elif [ "$5" = 1 ] && [ "$6" = 330 ] && [ "$8" = 0 ]; then
        # BTN_TOUCH and MT tracking often describe the same press.
        if [ "$7" = 1 ] && [ "$pressed" = 0 ] && [ "$mt_seen" = 0 ]; then
            pressed=1
            multiple=0
            source=button
            down_sec=$event_sec
            down_usec=$event_usec
            : > "$RUN/gesture-held" || exit 14
        elif [ "$7" = 0 ] && [ "$pressed" = 1 ] && [ "$source" = button ]; then
            gesture_finish
        fi
    fi
}
gesture_window() {
    waits=0
    while [ "$waits" -lt 30 ]; do
        [ ! -f "$RUN/manual-refresh" ] || return 0
        if [ "$waits" -ge 10 ] && [ ! -f "$RUN/gesture-held" ]; then return 0; fi
        run 3 sleep 1 || return "$?"
        waits=$((waits + 1))
    done
    if [ -f "$RUN/gesture-held" ]; then
        record 'GESTURE_TIMEOUT: contact held for 30 seconds; restoring UI.'
        : > "$RUN/stop-requested" || return 14
        exit 0
    fi
}
refresh_once() {
    refresh_kind=$1
    refresh_arg=--dedicated
    if [ "$refresh_kind" = manual ]; then refresh_arg=--dedicated-manual; fi
    record "REFRESH_START: kind=$refresh_kind slot=$slot"
    if run 80 /bin/sh "$RUN/refresh.sh" "$refresh_arg"; then
        record "REFRESH_EXIT=0 kind=$refresh_kind; see dedicated-refresh.log."
    else
        refresh_rc=$?
        record "REFRESH_EXIT=$refresh_rc kind=$refresh_kind; see dedicated-refresh.log."
        case "$refresh_rc" in
            12|13|30|31) record 'REFRESH_SKIPPED: cached image retained.' ;;
            15) must CACHE_RESTORE 15 /mnt/us/libkh/bin/fbink -q -c -f -w -V -g "file=$DIR/dashboard.png,w=-1,h=-1" ;;
            *) record 'REFRESH_FATAL: restoring reading interface.'; exit "$refresh_rc" ;;
        esac
    fi
}

for tool in awk cat cp date dd id initctl kdb lipc-get-prop lipc-set-prop \
    mkdir mktemp mv nohup od readlink rm rmdir setsid sleep tail wc; do
    command -v "$tool" >/dev/null 2>&1 ||
        { printf 'Missing dedicated-mode tool: %s\n' "$tool" >&2; exit 10; }
done
[ "$(id -u)" = 0 ] && [ -d "$DIR" ] && [ ! -L "$DIR" ] && [ ! -L "$RUN" ] &&
    [ ! -L "$LOG" ] || { printf 'Unsafe path or root unavailable.\n' >&2; exit 10; }

LOCK_LIB_DIR=$DIR
case "$0" in /tmp/calendar-dedicated/controller.sh) LOCK_LIB_DIR=$RUN ;; esac
[ -f "$LOCK_LIB_DIR/calendar-lock.sh" ] && [ ! -L "$LOCK_LIB_DIR/calendar-lock.sh" ] ||
    { printf 'LOCK_DEPENDENCY: calendar-lock.sh is missing or unsafe.\n' >&2; exit 10; }
. "$LOCK_LIB_DIR/calendar-lock.sh"
calendar_lock_require || exit "$?"
case "${1-}" in
    --run|--guard|--touch) calendar_lock_inherited || exit "$?" ;;
esac

# Recovery and the guardian must remain usable even if private config is lost.
case "${1-}" in
    --start|--run)
        CONFIG_DIR=$DIR
        if [ "$1" = --run ]; then CONFIG_DIR=$RUN; fi
        [ -f "$CONFIG_DIR/calendar-config.sh" ] && [ ! -L "$CONFIG_DIR/calendar-config.sh" ] ||
            { printf 'CONFIG_ERROR: calendar-config.sh is missing or unsafe.\n' >&2; exit 10; }
        . "$CONFIG_DIR/calendar-config.sh"
        calendar_load_config "$CONFIG_DIR/config.local.conf" || exit 10
        ;;
esac

case "${1-}" in
    --start)
        calendar_lock_legacy || exit "$?"
        calendar_lock_lifecycle || exit "$?"
        if [ -d "$RUN" ]; then
            if [ -r "$RUN/owner" ] && read -r pid start < "$RUN/owner" && alive "$pid" "$start"; then
                printf 'Dedicated dashboard already running.\n' >&2
                exit 11
            fi
            if [ -r "$RUN/guard" ] && read -r pid start < "$RUN/guard" && alive "$pid" "$start"; then
                printf 'Recovery guard still running; wait before retrying.\n' >&2
                exit 11
            fi
            [ -f "$RUN/restored" ] || { printf 'Recovery required: run the dedicated recovery entry.\n' >&2; exit 20; }
            # Only this controller's fixed runtime files are removed.
            for file in controller.sh refresh.sh owner guard guard-ready restored old-light old-sleep console.log child \
                deadline touch touch-child touch-ready touch-required touch-event touch.err stop-requested stopping \
                gesture-held manual-refresh touch-decoded calendar-config.sh config.local.conf calendar-lock.sh; do
                rm -f "$RUN/$file" || exit 14
            done
            # Per-process command captures are retained in the existing runtime directory.
        else
            mkdir "$RUN" || exit 11
        fi
        self=$(readlink -f "$0") || exit 14
        cp "$self" "$RUN/controller.sh" &&
            cp "$DIR/calendar-auto-refresh.sh" "$RUN/refresh.sh" &&
            cp "$DIR/calendar-config.sh" "$RUN/calendar-config.sh" &&
            cp "$DIR/calendar-lock.sh" "$RUN/calendar-lock.sh" &&
            cp "$DIR/config.local.conf" "$RUN/config.local.conf" || exit 14
        calendar_load_config "$RUN/config.local.conf" || exit 10
        # FD 9 crosses nohup/setsid/exec and remains held by the controller,
        # guardian and their descendants, including every publication gap.
        nohup setsid /bin/sh "$RUN/controller.sh" --run </dev/null > "$RUN/console.log" 2>&1 &
        printf 'Dedicated calendar requested: wake with power, tap to refresh; hold one finger for two seconds then release to exit.\n'
        exit 0
        ;;
    --stop|--recover)
        if calendar_lock_lifecycle; then
            :
        else
            lock_rc=$?
            [ "$lock_rc" = 32 ] || exit "$lock_rc"
            if [ -r "$RUN/owner" ] && read -r pid start < "$RUN/owner" &&
                alive "$pid" "$start" &&
                calendar_lock_has_lifecycle_fd "$pid" && alive "$pid" "$start"; then
                kill -USR1 "$pid" || exit 12
                printf 'Requested dashboard exit; the lock-owning controller will write the stop marker.\n'
                exit 0
            fi
            printf 'Startup, guardian or an inherited command is still active; wait and retry. No recovery files changed.\n' >&2
            exit 11
        fi
        [ -d "$RUN" ] || { printf 'No dedicated dashboard state exists.\n'; exit 0; }
        if read -r pid start < "$RUN/owner" && alive "$pid" "$start"; then
            : > "$RUN/stop-requested" || exit 14
            kill -TERM "$pid" || exit 12
            printf 'Requested dashboard exit and UI recovery.\n'
            exit 0
        fi
        if [ -r "$RUN/guard" ] && read -r pid start < "$RUN/guard" && alive "$pid" "$start"; then
            printf 'The independent guard is handling recovery; wait for Home.\n'
            exit 0
        fi
        restore
        exit "$?"
        ;;
    --touch)
        touch_owner=$2
        touch_owner_start=$3
        trap 'cancel_child' 0
        trap 'signal_exit 143' HUP INT TERM
        identity "$$" || exit 12
        printf '%s %s\n' "$$" "$proc_start" > "$RUN/touch" || exit 14
        exec 3</dev/input/event1 || exit 16
        pressed=0
        current_slot=0
        contacts=
        last_sec=
        mt_seen=0
        source=
        multiple=0
        : > "$RUN/touch-ready" || exit 14
        # Keep the descriptor open so SYN/release events cannot hide a later contact.
        while alive "$touch_owner" "$touch_owner_start"; do
            launching=1
            dd bs=4096 count=1 <&3 > "$RUN/touch-event" 2> "$RUN/touch.err" &
            child=$!
            child_start=
            if identity "$child"; then child_start=$proc_start; fi
            if ! printf '%s %s\n' "$child" "$child_start" > "$CHILD_FILE"; then
                honor_signal
                exit 14
            fi
            honor_signal
            wait "$child"
            touch_rc=$?
            child=
            child_start=
            rm -f "$CHILD_FILE"
            touch_bytes=$(wc -c < "$RUN/touch-event") || exit 16
            [ "$touch_rc" = 0 ] && [ "$touch_bytes" -gt 0 ] && [ "$((touch_bytes % 16))" = 0 ] || exit 16
            # Drain a batch per read rather than fork twice for every position event.
            od -An -v -tu2 "$RUN/touch-event" > "$RUN/touch-decoded" || exit 16
            while read -r fields; do
                gesture_event $fields
            done < "$RUN/touch-decoded"
        done
        exit 0
        ;;
    --guard)
        owner_pid=$2
        owner_start=$3
        identity "$$" || exit 12
        printf '%s %s\n' "$$" "$proc_start" > "$RUN/guard" || exit 14
        : > "$RUN/guard-ready" || exit 14
        while alive "$owner_pid" "$owner_start"; do
            [ ! -f "$RUN/restored" ] || exit 0
            now=$(date +%s) || break
            expired=0
            if ! read_number "$RUN/deadline" || [ "$now" -ge "$number" ] ||
                [ "$((number - now))" -gt 43320 ]; then expired=1; fi
            if [ -f "$RUN/touch-required" ] && [ ! -f "$RUN/stopping" ]; then
                if ! read -r touch_pid touch_start < "$RUN/touch" ||
                    ! alive "$touch_pid" "$touch_start"; then expired=1; fi
            fi
            if [ "$expired" = 1 ]; then
                kill -TERM "$owner_pid" 2>/dev/null
                for grace in 1 2 3 4 5 6 7 8 9 10 11 12; do
                    alive "$owner_pid" "$owner_start" || break
                    sleep 10
                done
                if alive "$owner_pid" "$owner_start"; then kill -KILL "$owner_pid" 2>/dev/null; fi
                break
            fi
            sleep 5
        done
        [ ! -f "$RUN/restored" ] || exit 0
        if [ -r "$RUN/child" ]; then
            read -r child child_start < "$RUN/child"
            cancel_child
        fi
        : > "$RUN/stopping"
        stop_touch
        log 'GUARD_RECOVERY: owner disappeared, operation deadline expired, or touch exit monitor failed.'
        restore
        exit "$?"
        ;;
    --run) ;;
    *) printf 'Expected --start or --recover.\n' >&2; exit 10 ;;
esac

identity "$$" || exit 12
owner_start=$proc_start
trap cleanup 0
trap '' HUP
trap 'signal_exit 130' INT
trap 'signal_exit 143' TERM
trap request_stop USR1
lease 180 || exit 14
printf '%s %s\n' "$$" "$owner_start" > "$RUN/owner" || exit 14
record 'START: dedicated schedule 06:30-22:00 UTC+8 every half hour; real kernel sleep between slots; no boot hooks.'
sleep 3
preflight || exit "$?"
nohup setsid /bin/sh "$RUN/controller.sh" --guard "$$" "$owner_start" </dev/null >/dev/null 2>&1 &
for attempt in 1 2 3 4 5; do
    [ ! -f "$RUN/guard-ready" ] || break
    sleep 1
done
[ -f "$RUN/guard-ready" ] && read -r guard_pid guard_start < "$RUN/guard" &&
    alive "$guard_pid" "$guard_start" || { record 'GUARD_FAILED: no state changes made.'; exit 12; }
: > "$RUN/sleep-owned" || exit 14
must SLEEP_CONTROL 4 lipc-set-prop -i com.lab126.powerd preventScreenSaver 1
property -i com.lab126.powerd preventScreenSaver && [ "$value" = 1 ] || exit 12
: > "$RUN/light-owned" || exit 14
must FRONTLIGHT_OFF 4 lipc-set-prop -i com.lab126.powerd flIntensity 0
: > "$RUN/ui-owned" || exit 14
# Framework teardown can signal its launcher; the independent guard remains alive.
critical=1
must GUI_STOP 40 initctl stop lab126_gui
sleep 2
for job in lab126_gui framework pillow webreader; do
    job_state "$job" || exit 12
    case "$job_value" in "$job stop/waiting"*) ;; *) record "GUI_STOP_FAILED: $job_value"; exit 12 ;; esac
done
critical=0
[ ! -f "$RUN/stop-requested" ] || exit 0
must CACHE_DISPLAY 15 /mnt/us/libkh/bin/fbink -q -c -f -w -V -g "file=$DIR/dashboard.png,w=-1,h=-1"
nohup setsid /bin/sh "$RUN/controller.sh" --touch "$$" "$owner_start" </dev/null >/dev/null 2>&1 &
for attempt in 1 2 3 4 5; do
    [ ! -f "$RUN/touch-ready" ] || break
    sleep 1
done
[ -f "$RUN/touch-ready" ] && read -r touch_pid touch_start < "$RUN/touch" &&
    alive "$touch_pid" "$touch_start" || { record 'TOUCH_MONITOR_FAILED: restoring UI.'; exit 16; }
: > "$RUN/touch-required" || exit 14
record 'RUNNING: wake with power; tap to refresh, hold one finger for 2 seconds then release to exit. Only explicit manual refresh bypasses quiet hours.'
no_sleep_count=0
while :; do
    schedule_now=$(date +%s) || exit 12
    next_slot "$schedule_now"
    slot=$next
    record "NEXT_SLOT: epoch=$slot; only an explicit tap may refresh before this slot."
    while :; do
        if [ -f "$RUN/manual-refresh" ]; then
            rm -f "$RUN/manual-refresh" || exit 14
            refresh_once manual
            continue
        fi
        if [ -f "$RUN/gesture-held" ]; then gesture_window || exit "$?"; continue; fi
        schedule_now=$(date +%s) || exit 12
        delay=$((slot - schedule_now))
        [ "$delay" -gt 0 ] || break
        if [ "$delay" -lt 6 ]; then
            must SLOT_WAIT "$((delay + 3))" sleep "$delay"
        else
            [ "$delay" -le 43200 ] || { record 'CLOCK_ERROR: unreasonable sleep interval.'; exit 12; }
            if sleep_once "$delay" 1; then
                no_sleep_count=0
            else
                sleep_rc=$?
                if [ "$sleep_rc" = 34 ]; then gesture_window || exit "$?"; continue; fi
                [ "$sleep_rc" = 33 ] || exit "$sleep_rc"
                no_sleep_count=$((no_sleep_count + 1))
                [ "$no_sleep_count" -lt 3 ] ||
                    { record 'SUSPEND_FAILED: three ineffective suspend attempts; restoring UI.'; exit 12; }
            fi
            schedule_now=$(date +%s) || exit 12
            if [ "$schedule_now" -lt "$slot" ]; then
                record 'EARLY_WAKE: tap to refresh or hold 2 seconds and release to exit; otherwise rearm the same slot.'
                gesture_window || exit "$?"
            fi
        fi
    done
    schedule_now=$(date +%s) || exit 12
    if [ "$((schedule_now - slot))" -gt 120 ]; then
        record "MISSED_SLOT: epoch=$slot; no catch-up download."
        continue
    fi
    refresh_once scheduled
done
