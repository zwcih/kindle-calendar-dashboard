#!/bin/sh

# Caller holds the display resource lock and supplies its own bounded runner,
# power-state gate, command output path and log(). No independent processes.
calendar_display_clock() {
    IFS=' ' read -r display_uptime display_rest < /proc/uptime || return 1
    display_now=${display_uptime%%.*}
    case "$display_now" in ''|*[!0-9]*) return 1 ;; esac
}

calendar_display_begin() {
    calendar_display_clock || {
        log 'DISPLAY_FAILED: monotonic clock unreadable.'
        return 15
    }
    CALENDAR_DISPLAY_DEADLINE=$((display_now + 30))
}

calendar_display_command() {
    display_stage=$1
    shift
    display_limit=$1
    shift
    calendar_display_clock || {
        log 'DISPLAY_FAILED: monotonic clock unreadable.'
        return 15
    }
    display_left=$((CALENDAR_DISPLAY_DEADLINE - display_now))
    [ "$display_left" -gt 0 ] || {
        log 'DISPLAY_FAILED: shared 30s image/status deadline exhausted.'
        return 15
    }
    [ "$display_limit" -le "$display_left" ] || display_limit=$display_left
    if calendar_display_run "$display_limit" "$@"; then
        # Some FBInk CLI versions log failed wait ioctls but still exit zero.
        # Quiet drawing must produce no diagnostics; fail closed on any output.
        if [ "$1" = "${FBINK-}" ] && [ "$display_stage" != capabilities ] &&
            [ -s "$CALENDAR_DISPLAY_OUTPUT" ]; then
            log "DISPLAY_FAILED: stage=$display_stage diagnostic output despite exit=0; output withheld." || return 14
            return 15
        fi
        return 0
    else
        display_rc=$?
    fi
    log "DISPLAY_FAILED: stage=$display_stage exit=$display_rc; command output withheld." || return 14
    case "$display_rc" in
        16|32|129|130|143) return "$display_rc" ;;
        *) return 15 ;;
    esac
}

calendar_status_valid_time() {
    case "$1" in
        [0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]' '[0-2][0-9]:[0-5][0-9]) ;;
        *) return 1 ;;
    esac
    status_year=${1%%-*}
    status_rest=${1#*-}
    status_month=${status_rest%%-*}
    status_rest=${status_rest#*-}
    status_day=${status_rest%% *}
    status_rest=${status_rest#* }
    status_hour=${status_rest%%:*}
    status_year=$((1$status_year - 10000))
    status_month=$((1$status_month - 100))
    status_day=$((1$status_day - 100))
    [ "$status_year" -ge 1970 ] && [ "$status_month" -ge 1 ] &&
        [ "$status_month" -le 12 ] && [ "$status_day" -ge 1 ] &&
        [ "$status_hour" -le 23 ] || return 1
    case "$status_month" in
        4|6|9|11) status_days=30 ;;
        2)
            status_days=28
            if [ "$((status_year % 4))" = 0 ] &&
                { [ "$((status_year % 100))" != 0 ] || [ "$((status_year % 400))" = 0 ]; }; then
                status_days=29
            fi
            ;;
        *) status_days=31 ;;
    esac
    [ "$status_day" -le "$status_days" ]
}

calendar_status_load() {
    CALENDAR_STATUS_TIME=--
    [ ! -L "$CALENDAR_STATUS_FILE" ] &&
        { [ ! -e "$CALENDAR_STATUS_FILE" ] || [ -f "$CALENDAR_STATUS_FILE" ]; } || {
        log 'STATUS_IO_FAILED: unsafe timestamp record; file not touched.'
        return 14
    }
    if [ ! -f "$CALENDAR_STATUS_FILE" ]; then
        log 'STATUS_TIME_UNKNOWN: no timestamp record for the cached PNG.' || return 14
        return 0
    fi
    status_size=$(wc -c < "$CALENDAR_STATUS_FILE") || {
        log 'STATUS_IO_FAILED: timestamp record size unreadable.'
        return 14
    }
    if [ "$status_size" -le 96 ]; then
        status_data=$(cat "$CALENDAR_STATUS_FILE") || {
            log 'STATUS_IO_FAILED: timestamp record unreadable.'
            return 14
        }
        status_hash=${status_data%%'
'*}
        status_time=${status_data#*'
'}
        if [ "${#status_hash}" = 64 ] && [ "$status_hash" = "$1" ] &&
            calendar_status_valid_time "$status_time"; then
            CALENDAR_STATUS_TIME=$status_time
            return 0
        fi
    fi
    log 'STATUS_TIME_UNKNOWN: timestamp record invalid or does not match cached PNG SHA; showing --.' || return 14
}

calendar_status_now() {
    if CALENDAR_STATUS_TIME=$(date '+%Y-%m-%d %H:%M') &&
        calendar_status_valid_time "$CALENDAR_STATUS_TIME"; then return 0; fi
    CALENDAR_STATUS_TIME=--
    log 'STATUS_CLOCK_FAILED: cannot record a valid local content display time.'
    return 14
}

calendar_display_require() {
    # Old -k implementations clear the entire screen: never probe by drawing.
    calendar_display_command capabilities 3 "$FBINK" --help || return "$?"
    if awk '
        /^[[:space:]]*-k, --cls (\[top=NUM,left=NUM,width=NUM,height=NUM\]|top=NUM,left=NUM,width=NUM,height=NUM)[[:space:]]*$/ {
            supported=1
        }
        END { exit !supported }
    ' "$CALENDAR_DISPLAY_OUTPUT"; then return 0; fi
    log 'DISPLAY_DEPENDENCY: FBInk help must advertise regional cls (official v1.21.0+); no drawing attempted.'
    return 15
}

calendar_display_image() {
    calendar_display_require || return "$?"
    calendar_display_ready || return "$?"
    calendar_display_command image 15 "$FBINK" -q -c -f -w -V \
        -g "file=$1,w=-1,h=-1" || return "$?"
    if [ "${2-}" = changed ]; then calendar_status_now || return "$?"; fi
    calendar_display_battery
}

calendar_display_battery() {
    calendar_display_command battery 3 lipc-get-prop -i com.lab126.powerd battLevel || return "$?"
    display_percent=$(cat "$CALENDAR_DISPLAY_OUTPUT") || {
        log 'DISPLAY_FAILED: battery result unreadable.'
        return 15
    }
    case "$display_percent" in
        0|[1-9]|[1-9][0-9]|100) ;;
        *) log 'DISPLAY_FAILED: battery must be an integer 0..100.'; return 15 ;;
    esac
    if [ "$CALENDAR_STATUS_TIME" != -- ] && ! calendar_status_valid_time "$CALENDAR_STATUS_TIME"; then
        log 'DISPLAY_FAILED: unsafe timestamp text.'
        return 14
    fi
    display_fill=$((display_percent * 25 / 100))
    display_label=$(printf '%3d%%' "$display_percent")
    calendar_display_ready || return "$?"
    calendar_display_command clear 3 "$FBINK" -q -b -B WHITE \
        -k top=1412,left=0,width=1072,height=36 || return "$?"
    # IBM 8x8 scaled by 3: FBInk adds 8 horizontal pixels at width 1072.
    calendar_display_command timestamp 3 "$FBINK" -q -b -V -F IBM -S 3 -C BLACK -B WHITE \
        -x 0 -y 0 -X 4 -Y 1418 "Updated $CALENDAR_STATUS_TIME" || return "$?"
    calendar_display_command outline 3 "$FBINK" -q -b -B BLACK \
        -k top=1421,left=918,width=32,height=18 || return "$?"
    calendar_display_command interior 3 "$FBINK" -q -b -B WHITE \
        -k top=1423,left=920,width=28,height=14 || return "$?"
    calendar_display_command terminal 3 "$FBINK" -q -b -B BLACK \
        -k top=1426,left=951,width=3,height=8 || return "$?"
    # A zero-sized cls region may mean full screen, never an empty fill.
    if [ "$display_fill" -gt 0 ]; then
        calendar_display_command fill 3 "$FBINK" -q -b -B BLACK \
            -k "top=1425,left=922,width=$display_fill,height=10" || return "$?"
    fi
    calendar_display_command percent 3 "$FBINK" -q -b -V -F IBM -S 3 -C BLACK -B WHITE \
        -x 0 -y 0 -X 956 -Y 1418 "$display_label" || return "$?"
    calendar_display_command refresh 5 "$FBINK" -q -w -W GC16 \
        -s top=1412,left=0,width=1072,height=36
}
