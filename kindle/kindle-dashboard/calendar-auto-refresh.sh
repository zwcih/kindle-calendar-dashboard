#!/bin/sh

# One invocation, one refresh. Scheduling and suspend/wake supervision are external.
PATH=/usr/bin:/bin:/usr/sbin:/sbin
TZ=CST-8
export PATH TZ
umask 077
set -f

DIR=/mnt/us/kindle-dashboard
IMAGE=$DIR/dashboard.png
CALENDAR_STATUS_FILE=$DIR/dashboard.status
LOG=$DIR/auto-refresh.log
TRIAL=/tmp/calendar-dedicated-trial
dedicated_trial=0
manual_refresh=0
standalone_manual=0
FBINK=/mnt/us/libkh/bin/fbink
CONFIG_DIR=$DIR
case "$0" in
    /tmp/calendar-dedicated/refresh.sh) CONFIG_DIR=/tmp/calendar-dedicated ;;
esac
[ -f "$CONFIG_DIR/calendar-config.sh" ] && [ ! -L "$CONFIG_DIR/calendar-config.sh" ] ||
    { printf 'CONFIG_ERROR: calendar-config.sh is missing or unsafe.\n' >&2; exit 10; }
. "$CONFIG_DIR/calendar-config.sh"
calendar_load_config "$CONFIG_DIR/config.local.conf" || exit 10
[ -f "$CONFIG_DIR/calendar-lock.sh" ] && [ ! -L "$CONFIG_DIR/calendar-lock.sh" ] ||
    { printf 'LOCK_DEPENDENCY: calendar-lock.sh is missing or unsafe.\n' >&2; exit 10; }
. "$CONFIG_DIR/calendar-lock.sh"
calendar_lock_require || exit "$?"
[ -f "$CONFIG_DIR/calendar-display.sh" ] && [ ! -L "$CONFIG_DIR/calendar-display.sh" ] ||
    { printf 'DISPLAY_DEPENDENCY: calendar-display.sh is missing or unsafe.\n' >&2; exit 10; }
. "$CONFIG_DIR/calendar-display.sh"
URL=$IMAGE_URL
WORK=
child_pid=
child_start=
launch_guard=0
pending_signal=0
cleaning=0
log_failed=0
radio_owned=0
wifi_owned=0
old_radio=
old_wifi=
deadline=0
network_phase=network

report() { printf '%s\n' "$*" >&2; }

# Use monotonic uptime for budgets; a broken wall clock cannot extend polling.
monotime() {
    IFS=' ' read -r uptime_value uptime_rest < /proc/uptime || return 1
    mono=${uptime_value%%.*}
    case "$mono" in ''|*[!0-9]*) return 1 ;; esac
}

identity() {
    [ -r "/proc/$1/stat" ] || return 1
    IFS= read -r proc_stat < "/proc/$1/stat" || return 1
    proc_fields=${proc_stat##*) }
    [ "$proc_fields" != "$proc_stat" ] || return 1
    set -- $proc_fields
    [ "$#" -ge 20 ] || return 1
    proc_state=$1
    shift 19
    proc_start=$1
    case "$proc_start" in ''|*[!0-9]*) return 1 ;; esac
}

same_child() {
    [ -n "$child_pid" ] && [ -n "$child_start" ] || return 1
    identity "$child_pid" || return 1
    [ "$proc_start" = "$child_start" ]
}

child_running() {
    same_child || return 1
    [ "$proc_state" != Z ] && [ "$proc_state" != X ]
}

cancel_child() {
    # No grace sleep: cancellation must not consume the restoration budget.
    if same_child; then
        kill -TERM "$child_pid" 2>/dev/null
        if same_child; then
            kill -KILL "$child_pid" 2>/dev/null
        fi
    fi
    if [ -n "$child_pid" ]; then
        wait "$child_pid" 2>/dev/null
    fi
    child_pid=
    child_start=
}

on_signal() {
    [ "$cleaning" = 0 ] || return
    pending_signal=$1
    # Finish recording the exact child identity before allowing EXIT cleanup.
    [ "$launch_guard" = 0 ] && exit "$pending_signal"
    return 0
}

honor_signal() {
    launch_guard=0
    [ "$pending_signal" = 0 ] || exit "$pending_signal"
}

# Atomic replacement keeps the persistent log at or below 64 KiB on every write.
# Only the lock owner writes it. The directory is assumed not to be hostile.
log() {
    if [ -z "$WORK" ] || [ -L "$LOG" ] ||
        { [ -e "$LOG" ] && [ ! -f "$LOG" ]; }; then
        log_failed=1
        report "LOG_FAILED: $*"
        return 1
    fi
    stamp=$(date '+%Y-%m-%d %H:%M:%S %z')
    if [ "$?" -ne 0 ]; then
        stamp=CLOCK_UNAVAILABLE
        log_failed=1
    fi
    if ! printf '[%s] %s\n' "$stamp" "$*" > "$WORK/log.line"; then
        log_failed=1
        report "LOG_FAILED: $*"
        return 1
    fi
    if [ -f "$LOG" ]; then
        tail -c 61440 "$LOG" > "$WORK/log.next"
    else
        : > "$WORK/log.next"
    fi
    if [ "$?" -ne 0 ] ||
        ! tail -c 4096 "$WORK/log.line" >> "$WORK/log.next" ||
        ! mv -f "$WORK/log.next" "$LOG"; then
        log_failed=1
        report "LOG_FAILED: $*"
        return 1
    fi
}

# All lipc/curl/fbink commands are direct children, with an individual deadline
# inside the current phase deadline and an independent finite polling counter.
bounded() {
    command_limit=$1
    shift
    : > "$WORK/output" || return 14
    monotime || return 125
    # Reserve one second for integer-uptime rounding and the one-second poll.
    phase_stop=$((deadline - 1))
    [ "$mono" -lt "$phase_stop" ] || return 124
    command_deadline=$((mono + command_limit - 1))
    [ "$command_deadline" -le "$phase_stop" ] || command_deadline=$phase_stop
    polls_left=$((command_limit + 2))
    launch_guard=1
    "$@" > "$WORK/output" 2>&1 &
    child_pid=$!
    child_start=
    if identity "$child_pid"; then
        child_start=$proc_start
    fi
    if [ "$cleaning" = 0 ]; then honor_signal; else launch_guard=0; fi
    while child_running; do
        if ! monotime; then
            cancel_child
            return 125
        fi
        if [ "$mono" -ge "$command_deadline" ] || [ "$polls_left" -le 0 ]; then
            cancel_child
            return 124
        fi
        polls_left=$((polls_left - 1))
        if ! sleep 1; then
            cancel_child
            return 125
        fi
    done
    wait "$child_pid"
    command_status=$?
    child_pid=
    child_start=
    return "$command_status"
}

calendar_display_run() {
    bounded "$@"
}

calendar_display_ready() {
    window_gate || { log 'CANCEL: rendering outside allowed window or clock unreadable.'; exit 32; }
    check_sleep
}

display_refresh() {
    if [ "$new_hash" = "$old_hash" ] && [ "$standalone_manual" = 0 ]; then
        log "UNCHANGED: sha256=$new_hash; no display, battery sample or timestamp replacement."
        return "$?"
    fi
    window_gate || { log 'CANCEL: rendering outside allowed window or clock unreadable.'; return 32; }
    monotime || { log 'RENDER_FAILED: monotonic clock unreadable.'; return 15; }
    deadline=$((mono + 30))
    CALENDAR_DISPLAY_DEADLINE=$deadline
    CALENDAR_DISPLAY_OUTPUT=$WORK/output
    calendar_status_load "$old_hash" || return "$?"
    display_change=unchanged
    if [ "$new_hash" != "$old_hash" ]; then display_change=changed; fi
    calendar_display_image "$WORK/download.png" "$display_change" || return "$?"
    if [ "$display_change" = changed ]; then
        if ! printf '%s\n%s\n' "$new_hash" "$CALENDAR_STATUS_TIME" > "$WORK/status.next"; then
            log 'STATUS_IO_FAILED: timestamp staging failed; cache and previous record retained; display may be provisional.'
            return 14
        fi
    fi
    if [ -L "$IMAGE" ] || { [ -e "$IMAGE" ] && [ ! -f "$IMAGE" ]; } ||
        ! mv -f "$WORK/download.png" "$IMAGE"; then
        log 'IO_FAILED: display succeeded but atomic image commit failed.'
        return 14
    fi
    if [ "$display_change" = changed ] &&
        ! mv -f "$WORK/status.next" "$CALENDAR_STATUS_FILE"; then
        log 'STATUS_IO_FAILED: PNG committed but timestamp publication failed; mismatched record must display --; refresh not fully successful.'
        return 14
    fi
    log "UPDATED: image and battery displayed; original PNG atomically committed; sha256=$new_hash bytes=$size"
}

window_gate() {
    if [ "$manual_refresh" = 1 ]; then window_left=86400; return 0; fi
    wall=$(date '+%H%M%S') || return 1
    case "$wall" in
        [0-2][0-9][0-5][0-9][0-5][0-9]) ;;
        *) return 1 ;;
    esac
    [ "$wall" -ge 63000 ] && [ "$wall" -le 220159 ] || return 1
    wall_h=${wall%????}
    wall_m=${wall#??}
    wall_m=${wall_m%??}
    wall_s=${wall#????}
    # Prefixing with 1 avoids POSIX shell octal interpretation of 08 and 09.
    window_left=$((79320 - ((1$wall_h - 100) * 3600 + (1$wall_m - 100) * 60 + 1$wall_s - 100)))
}

network_gate() {
    window_gate || { log 'CANCEL: outside 06:30:00..22:01:59 UTC+8 or clock unreadable.'; exit 32; }
    monotime || { log 'NETWORK_FAILED: monotonic clock unreadable.'; exit 12; }
    window_deadline=$((mono + window_left))
    [ "$deadline" -le "$window_deadline" ] || deadline=$window_deadline
    [ "$mono" -lt "$deadline" ] || { log "NETWORK_FAILED: $network_phase deadline exhausted within shared 60s budget."; exit 12; }
}

dedicated_owner_alive() {
    [ -d "$TRIAL" ] && [ ! -L "$TRIAL" ] &&
        [ -f "$TRIAL/owner" ] && [ ! -L "$TRIAL/owner" ] &&
        [ -f "$TRIAL/ui-owned" ] && [ ! -L "$TRIAL/ui-owned" ] &&
        [ -f "$TRIAL/sleep-owned" ] && [ ! -L "$TRIAL/sleep-owned" ] || return 1
    trial_owner=$(cat "$TRIAL/owner") || return 1
    set -- $trial_owner
    [ "$#" = 2 ] || return 1
    case "$1" in ''|*[!0-9]*) return 1 ;; esac
    case "$2" in ''|*[!0-9]*) return 1 ;; esac
    [ "$1" = "$PPID" ] || return 1
    identity "$1" || return 1
    [ "$proc_start" = "$2" ] &&
        [ "$proc_state" != Z ] && [ "$proc_state" != X ]
}

standalone_available() {
    for session in /tmp/calendar-dedicated /tmp/calendar-dedicated-trial; do
        [ ! -L "$session" ] || return 1
        if [ -e "$session" ]; then
            [ -d "$session" ] && [ -f "$session/restored" ] || return 1
            if [ -f "$session/owner" ]; then
                read -r session_pid session_start < "$session/owner" || return 1
                case "$session_pid:$session_start" in *[!0-9:]*|:*|*:) return 1 ;; esac
                if identity "$session_pid" && [ "$proc_start" = "$session_start" ] &&
                    [ "$proc_state" != Z ] && [ "$proc_state" != X ]; then return 1; fi
            fi
        fi
    done
}

check_sleep() {
    expected_state=screenSaver
    if [ "$standalone_manual" = 1 ]; then
        standalone_available ||
            { log 'CANCEL: exit dedicated mode and complete recovery before standalone refresh.'; exit 32; }
        expected_state=active
    fi
    if [ "$dedicated_trial" = 1 ]; then
        dedicated_owner_alive ||
            { log 'CANCEL: dedicated trial parent identity or ownership flags lost.'; exit 32; }
        if bounded 3 lipc-get-prop -i com.lab126.powerd preventScreenSaver; then
            prevented=$(cat "$WORK/output") ||
                { log 'CANCEL: dedicated trial sleep inhibition result unreadable.'; exit 32; }
        else
            prevent_rc=$?
            log "CANCEL: dedicated trial preventScreenSaver unavailable, exit=$prevent_rc."
            exit 32
        fi
        [ "$prevented" = 1 ] ||
            { log "CANCEL: dedicated trial preventScreenSaver=$prevented."; exit 32; }
        expected_state=active
    fi
    if bounded 3 lipc-get-prop com.lab126.powerd state; then
        state=$(cat "$WORK/output") || {
            if [ "$dedicated_trial" = 1 ]; then
                log 'CANCEL: dedicated trial power state result unreadable.'
                exit 32
            fi
            log 'IO_FAILED: cannot read power state result.'
            exit 14
        }
    else
        state_rc=$?
        log "CANCEL: powerd.state unavailable, exit=$state_rc."
        exit 32
    fi
    [ "$state" = "$expected_state" ] || { log "CANCEL: powerd.state=$state; no display operation."; exit 32; }
    if [ "$dedicated_trial" = 1 ]; then
        dedicated_owner_alive ||
            { log 'CANCEL: dedicated trial parent identity or ownership flags lost during state queries.'; exit 32; }
    fi
}

connection_ready() {
    if bounded 3 lipc-get-prop com.lab126.wifid cmState; then
        cm_state=$(cat "$WORK/output") || exit 14
    else
        link_rc=$?
        log "NETWORK_FAILED: cmState query exit=$link_rc."
        exit 12
    fi
    if [ "$cm_state" != CONNECTED ]; then
        log "WIFI_WAIT: cmState=$cm_state; enabled switches alone are not connectivity."
        return 1
    fi
    if ! IFS= read -r carrier < /sys/class/net/wlan0/carrier; then
        log 'NETWORK_FAILED: wlan0 carrier unreadable.'
        exit 12
    fi
    case "$carrier" in
        0) log 'WIFI_WAIT: cmState=CONNECTED but carrier=0.'; return 1 ;;
        1) ;;
        *) log 'NETWORK_FAILED: invalid wlan0 carrier.'; exit 12 ;;
    esac
    if ! bounded 3 ifconfig wlan0; then
        log 'NETWORK_FAILED: cannot query wlan0 address.'
        exit 12
    fi
    ipv4=$(awk '
        $1 == "inet" {
            ip=$2
            sub(/^addr:/, "", ip)
            n=split(ip, a, /[.]/)
            if (n != 4) next
            valid=1
            for (i=1; i<=4; i++)
                if (a[i] !~ /^[0-9]+$/ || a[i]+0 > 255) valid=0
            if (valid && a[1]+0 > 0 && a[1]+0 < 224 && a[1]+0 != 127 &&
                !(a[1]+0 == 169 && a[2]+0 == 254)) { print ip; exit }
        }
    ' "$WORK/output") || { log 'NETWORK_FAILED: IPv4 parsing failed.'; exit 12; }
    default_route=$(awk '
        $1 == "wlan0" && $2 == "00000000" && $8 == "00000000" &&
            $4 ~ /[13579bBdDfF]$/ { found=1 }
        END { print found+0 }
    ' /proc/net/route) || { log 'NETWORK_FAILED: route table unreadable.'; exit 12; }
    if [ -z "$ipv4" ] || [ "$default_route" != 1 ]; then
        log "WIFI_WAIT: cmState=CONNECTED carrier=1 ipv4=${ipv4:-none} default_route=$default_route"
        return 1
    fi
    log "WIFI_READY: cmState=CONNECTED carrier=1 ipv4=$ipv4 default_route=1"
}

restore_one() {
    if bounded 4 lipc-set-prop -i "$1" "$2" "$3"; then
        log "WIFI_RESTORE_SET: $1 $2=$3"
    else
        restore_rc=$?
        restore_failed=1
        log "WIFI_RESTORE_SET_FAILED: $1 $2=$3 exit=$restore_rc"
    fi
}

verify_one() {
    if bounded 4 lipc-get-prop -i "$1" "$2"; then
        restored=$(cat "$WORK/output")
        if [ "$?" -ne 0 ] || [ "$restored" != "$3" ]; then
            restore_failed=1
            log "WIFI_RESTORE_MISMATCH: $1 $2 expected=$3 actual=$restored"
        fi
    else
        restore_rc=$?
        restore_failed=1
        log "WIFI_RESTORE_VERIFY_FAILED: $1 $2 exit=$restore_rc"
    fi
}

cleanup() {
    final_status=$?
    [ "$pending_signal" = 0 ] || final_status=$pending_signal
    cleaning=1
    trap - 0
    trap '' HUP INT TERM
    cancel_child
    restore_failed=0
    if [ "$radio_owned" = 1 ] || [ "$wifi_owned" = 1 ]; then
        if monotime; then
            deadline=$((mono + 20))
            if [ "$old_radio" = 1 ]; then
                [ "$radio_owned" = 0 ] || restore_one com.lab126.cmd wirelessEnable "$old_radio"
                [ "$wifi_owned" = 0 ] || restore_one com.lab126.wifid enable "$old_wifi"
            else
                [ "$wifi_owned" = 0 ] || restore_one com.lab126.wifid enable "$old_wifi"
                [ "$radio_owned" = 0 ] || restore_one com.lab126.cmd wirelessEnable "$old_radio"
            fi
            verify_one com.lab126.cmd wirelessEnable "$old_radio"
            verify_one com.lab126.wifid enable "$old_wifi"
        else
            restore_failed=1
            log 'WIFI_RESTORE_FAILED: monotonic clock unreadable; cannot safely bound restoration.'
        fi
        if [ "$restore_failed" = 0 ]; then
            log "WIFI_RESTORED: wirelessEnable=$old_radio wifiEnable=$old_wifi"
        else
            log "RECOVERY_REQUIRED: original wirelessEnable=$old_radio wifiEnable=$old_wifi; operation_exit=$final_status"
            report "RECOVERY_REQUIRED: restore wirelessEnable=$old_radio wifiEnable=$old_wifi"
            case "$final_status" in 129|130|143) ;; *) final_status=20 ;; esac
        fi
    fi
    if [ -n "$WORK" ]; then
        log "END: exit=$final_status restore_failed=$restore_failed log_failed=$log_failed"
        if [ "$log_failed" = 1 ]; then
            case "$final_status" in 129|130|143|20) ;; *) final_status=14 ;; esac
            report "LOG_IO_FAILED: final exit=$final_status"
        fi
        if ! rm -f "$WORK/output" "$WORK/download.png" "$WORK/curl.err" "$WORK/status.next" \
            "$WORK/log.line" "$WORK/log.next" || ! rmdir "$WORK"; then
            report "CLEANUP_FAILED: owned workspace $WORK"
            case "$final_status" in 129|130|143|20) ;; *) final_status=14 ;; esac
        fi
    fi
    # Close only our references. An orphan command must retain exclusion.
    exec 8>&-
    exec 9>&-
    exit "$final_status"
}

trap cleanup 0
trap 'on_signal 129' HUP
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

case "$#" in
    0) ;;
    1)
        case "$1" in
            --dedicated) TRIAL=/tmp/calendar-dedicated ;;
            --dedicated-manual) TRIAL=/tmp/calendar-dedicated; manual_refresh=1 ;;
            --dedicated-trial) ;;
            --manual) standalone_manual=1; manual_refresh=1 ;;
            *) report 'DEPENDENCY/USAGE: expected --manual, --dedicated or --dedicated-manual (legacy: no arguments, --dedicated-trial).'; exit 10 ;;
        esac
        if [ "$standalone_manual" = 1 ]; then
            LOG=$DIR/manual-refresh.log
        else
            dedicated_trial=1
            LOG=$DIR/dedicated-refresh.log
        fi
        ;;
    *) report 'DEPENDENCY/USAGE: expected --manual, --dedicated or --dedicated-manual (legacy: no arguments, --dedicated-trial).'; exit 10 ;;
esac
for tool in awk cat curl date ifconfig lipc-get-prop lipc-set-prop mkdir mktemp mv od readlink \
    rm rmdir sha256sum sleep tail tr wc; do
    command -v "$tool" >/dev/null 2>&1 ||
        { report "DEPENDENCY_MISSING: $tool"; exit 10; }
done
[ -x "$FBINK" ] || { report "DEPENDENCY_MISSING: executable $FBINK"; exit 10; }
identity "$$" && monotime || { report 'DEPENDENCY_MISSING: readable Linux /proc stat/uptime.'; exit 10; }
[ -d "$DIR" ] && [ ! -L "$DIR" ] && [ ! -L "$IMAGE" ] && [ ! -L "$LOG" ] ||
    { report 'IO_FAILED: missing dashboard directory or symlink target.'; exit 14; }
for target in "$IMAGE" "$LOG"; do
    [ ! -e "$target" ] || [ -f "$target" ] ||
        { report "IO_FAILED: not a regular file: $target"; exit 14; }
done
launch_guard=1
calendar_lock_legacy || exit "$?"
if [ "$dedicated_trial" = 1 ] && [ "$TRIAL" = /tmp/calendar-dedicated ]; then
    dedicated_owner_alive && calendar_lock_inherited ||
        { report 'CANCEL: dedicated worker must be a direct authorized child with an inherited lifecycle lock.'; exit 32; }
else
    calendar_lock_lifecycle || exit "$?"
fi
calendar_lock_resource || exit "$?"
honor_signal
launch_guard=1
WORK=$(mktemp -d "$DIR/.calendar-auto-refresh.XXXXXX") ||
    { report 'IO_FAILED: cannot create same-filesystem workspace.'; exit 14; }
honor_signal
log 'START: single refresh; no UI pause, sleep inhibition, RTC or boot changes.' || exit 14
if [ "$dedicated_trial" = 1 ]; then
    monotime || { log 'CANCEL: dedicated trial monotonic clock unreadable.'; exit 32; }
    deadline=$((mono + 8))
    check_sleep
fi
if [ "$manual_refresh" = 1 ]; then
    log 'MANUAL_REQUEST: explicit refresh; quiet hours bypassed, mode authorization and battery protection unchanged.'
fi
window_gate || { log 'CANCEL: outside 06:30:00..22:01:59 UTC+8 or clock unreadable.'; exit 32; }
monotime || { log 'NETWORK_FAILED: monotonic clock unreadable.'; exit 12; }
deadline=$((mono + 60))
network_gate

if bounded 3 lipc-get-prop -i com.lab126.powerd battLevel; then
    battery=$(cat "$WORK/output") || exit 14
else
    battery_rc=$?
    log "BATTERY_UNREADABLE: battLevel exit=$battery_rc"
    exit 31
fi
if bounded 3 lipc-get-prop -i com.lab126.powerd isCharging; then
    charging=$(cat "$WORK/output") || exit 14
else
    battery_rc=$?
    log "BATTERY_UNREADABLE: isCharging exit=$battery_rc"
    exit 31
fi
case "$battery" in
    0|[1-9]|[1-9][0-9]|100) ;;
    *) log 'BATTERY_UNREADABLE: battLevel must be an integer 0..100.'; exit 31 ;;
esac
case "$charging" in
    0|1) ;;
    *) log 'BATTERY_UNREADABLE: isCharging must be 0 or 1.'; exit 31 ;;
esac
log "BATTERY: percent=$battery charging=$charging"
if [ "$battery" -le 20 ] && [ "$charging" = 0 ]; then
    log 'LOW_BATTERY: no network or display operation.'
    exit 30
fi
check_sleep

if bounded 3 lipc-get-prop -i com.lab126.cmd wirelessEnable; then
    old_radio=$(cat "$WORK/output") || exit 14
else
    query_rc=$?
    log "NETWORK_FAILED: wirelessEnable snapshot exit=$query_rc"
    exit 12
fi
if bounded 3 lipc-get-prop -i com.lab126.wifid enable; then
    old_wifi=$(cat "$WORK/output") || exit 14
else
    query_rc=$?
    log "NETWORK_FAILED: wifid enable snapshot exit=$query_rc"
    exit 12
fi
for value in "$old_radio" "$old_wifi"; do
    case "$value" in
        0|1) ;;
        *) log 'NETWORK_FAILED: invalid initial Wi-Fi state; nothing changed.'; exit 12 ;;
    esac
done
log "WIFI_SNAPSHOT: wirelessEnable=$old_radio wifiEnable=$old_wifi"
[ "$old_radio" = "$old_wifi" ] ||
    { log 'NETWORK_FAILED: inconsistent initial Wi-Fi switches; leaving settings unchanged.'; exit 12; }
network_gate
check_sleep
# KOReader's Kindle restoreWifiAsync reasserts both switches even when already 1.
[ "$old_radio" != 0 ] || radio_owned=1
if bounded 4 lipc-set-prop -i com.lab126.cmd wirelessEnable 1; then
    log "WIFI_ENABLE_REQUEST: wirelessEnable=1 previous=$old_radio"
else
    set_rc=$?
    log "NETWORK_FAILED: requesting wirelessEnable exit=$set_rc"
    exit 12
fi
network_gate
check_sleep
[ "$old_wifi" != 0 ] || wifi_owned=1
if bounded 4 lipc-set-prop -i com.lab126.wifid enable 1; then
    log "WIFI_ENABLE_REQUEST: wifiEnable=1 previous=$old_wifi; using configured network only."
else
    set_rc=$?
    log "NETWORK_FAILED: requesting wifid enable exit=$set_rc"
    exit 12
fi

network_gate
check_sleep
if ! connection_ready; then
    network_gate
    check_sleep
    if bounded 4 lipc-set-prop -s com.lab126.cmd ensureConnection "wifi:$WIFI_SSID"; then
        log 'CONNECTION_REQUEST: ensureConnection for the configured saved Wi-Fi network accepted; awaiting actual connectivity.'
    else
        connect_rc=$?
        log "NETWORK_FAILED: ensureConnection exit=$connect_rc; private command output withheld."
        exit 12
    fi
fi
network_gate
network_deadline=$deadline
link_deadline=$((mono + 20))
[ "$link_deadline" -ge "$deadline" ] || deadline=$link_deadline
network_phase=connection
ready=0
for link_attempt in 1 2 3 4 5 6 7 8 9 10; do
    network_gate
    [ "$((deadline - mono))" -gt 4 ] || break
    check_sleep
    if connection_ready; then ready=1; break; fi
    monotime || { log 'NETWORK_FAILED: monotonic clock unreadable.'; exit 12; }
    [ "$((deadline - mono))" -gt 3 ] || break
    sleep 1 || { log 'NETWORK_FAILED: connection wait interrupted.'; exit 12; }
done
[ "$ready" = 1 ] ||
    { log 'NETWORK_FAILED: Wi-Fi not ready within connection budget; no HTTP request made.'; exit 12; }
deadline=$network_deadline
network_phase=network

attempt=0
downloaded=0
while [ "$attempt" -lt 60 ]; do
    network_gate
    check_sleep
    if [ "$attempt" -gt 0 ]; then
        connection_ready ||
            { log 'NETWORK_FAILED: connection lost; stopping download retries.'; exit 12; }
    fi
    network_gate
    attempt=$((attempt + 1))
    remaining=$((deadline - mono))
    attempt_limit=$remaining
    [ "$attempt_limit" -le 15 ] || attempt_limit=15
    : > "$WORK/curl.err" || { log 'IO_FAILED: cannot prepare curl error output.'; exit 14; }
    # -q MUST be first: do not inherit an insecure ~/.curlrc.
    if bounded "$attempt_limit" curl -q --globoff --fail --location --max-redirs 5 \
        --silent --show-error --proto '=https' --proto-redir '=https' \
        --connect-timeout 5 --max-time "$attempt_limit" --retry 0 \
        --max-filesize 5242880 --stderr "$WORK/curl.err" \
        --output "$WORK/download.png" --write-out '%{http_code}\n%{content_type}\n' "$URL"; then
        downloaded=1
        break
    else
        curl_rc=$?
    fi
    log "DOWNLOAD_FAILED: attempt=$attempt exit=$curl_rc; private curl output withheld."
    case "$curl_rc" in
        5|6|7|28|52|55|56|124) ;;
        *) log 'NETWORK_FAILED: non-retryable error; cached image preserved.'; exit 12 ;;
    esac
    network_gate
    [ "$((deadline - mono))" -gt 1 ] ||
        { log 'NETWORK_FAILED: insufficient retry budget.'; exit 12; }
    sleep 1 || { log 'NETWORK_FAILED: retry delay failed.'; exit 12; }
done
[ "$downloaded" = 1 ] || { log 'NETWORK_FAILED: retry count exhausted.'; exit 12; }
http=$(awk 'NR == 1 { print }' "$WORK/output") || exit 14
type=$(awk 'NR == 2 { sub(/;.*/, ""); print }' "$WORK/output") || exit 14
log "HTTP: status=$http type=$type attempts=$attempt"
[ "$http" = 200 ] || { log 'NETWORK_FAILED: final HTTP status is not 200.'; exit 12; }
[ "$type" = image/png ] && [ -s "$WORK/download.png" ] ||
    { log 'PNG_FAILED: expected a nonempty image/png response.'; exit 13; }
size=$(wc -c < "$WORK/download.png") || exit 14
case "$size" in ''|*[!0-9\ ]*) log 'IO_FAILED: invalid download byte count.'; exit 14 ;; esac
[ "$size" -ge 45 ] && [ "$size" -le 5242880 ] ||
    { log 'PNG_FAILED: size outside 45 bytes..5 MiB.'; exit 13; }
raw=$(od -An -tx1 -N29 "$WORK/download.png") || { log 'PNG_FAILED: header unreadable.'; exit 13; }
header=$(printf '%s' "$raw" | tr -d '[:space:]') || exit 13
# Signature, IHDR length/type, 1072x1448, depth 8, grayscale, standard methods.
case "$header" in
    89504e470d0a1a0a0000000d4948445200000430000005a80800000000|\
    89504e470d0a1a0a0000000d4948445200000430000005a80800000001) ;;
    *) log 'PNG_FAILED: expected 1072x1448 8-bit grayscale PNG IHDR.'; exit 13 ;;
esac
new_sum=$(sha256sum "$WORK/download.png") || { log 'IO_FAILED: new image hash failed.'; exit 14; }
new_hash=${new_sum%% *}
old_hash=
if [ -L "$IMAGE" ] || { [ -e "$IMAGE" ] && [ ! -f "$IMAGE" ]; }; then
    log 'IO_FAILED: cached image became a symlink or nonregular file.'
    exit 14
fi
if [ -f "$IMAGE" ]; then
    old_sum=$(sha256sum "$IMAGE") || { log 'IO_FAILED: cached image hash failed.'; exit 14; }
    old_hash=${old_sum%% *}
fi
for hash in "$new_hash" "${old_hash:-$new_hash}"; do
    case "$hash" in ''|*[!0-9a-f]*) log 'IO_FAILED: invalid SHA-256 output.'; exit 14 ;; esac
    [ "${#hash}" = 64 ] || { log 'IO_FAILED: incomplete SHA-256 output.'; exit 14; }
done
display_refresh
exit "$?"
