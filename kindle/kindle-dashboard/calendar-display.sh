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

calendar_display_geometry() {
    for display_attribute in /sys/class/graphics/fb0/virtual_size \
        /sys/class/graphics/fb0/rotate /sys/class/graphics/fb0/bits_per_pixel; do
        display_field=${display_attribute##*/}
        display_value=unavailable
        if [ -f "$display_attribute" ] && [ -r "$display_attribute" ] &&
            calendar_display_clock && [ "$display_now" -lt "$CALENDAR_DISPLAY_DEADLINE" ]; then
            # Optional diagnostics share the original deadline, never a new budget.
            if calendar_display_run 1 awk -v field="$display_field" '
                NR == 1 && length($0) <= 32 &&
                    ((field == "virtual_size" && /^[0-9]+,[0-9]+$/) ||
                     (field != "virtual_size" && /^[0-9]+$/)) { print; valid=1; exit }
                { exit }
                END { exit !valid }
            ' "$display_attribute"; then
                display_value=$(cat "$CALENDAR_DISPLAY_OUTPUT") || display_value=unavailable
            fi
        fi
        printf '%s=%s\n' "$display_field" "$display_value" || return 14
    done
}

calendar_display_save_failure() {
    display_error_file=$DIR/display-refresh-error.log
    if [ -L "$display_error_file" ] ||
        { [ -e "$display_error_file" ] && [ ! -f "$display_error_file" ]; }; then
        log 'DISPLAY_DIAGNOSTIC_FAILED: unsafe private diagnostic target; not touched.'
        return 14
    fi
    display_error_tmp=$(mktemp "$DIR/.display-refresh-error.XXXXXX") || {
        log 'DISPLAY_DIAGNOSTIC_FAILED: cannot stage private diagnostic.'
        return 14
    }
    if ! printf 'stage=%s original_exit=%s recorder_pid=%s\nargv=%s\nscreenWidth=%s screenHeight=%s currentRota=%s\n--- last 3000 output bytes ---\n' \
        "$2" "$1" "$$" "$3" "${display_width:-unavailable}" "${display_height:-unavailable}" \
        "${display_rotation:-unavailable}" > "$display_error_tmp" ||
        ! tail -c 3000 "$CALENDAR_DISPLAY_OUTPUT" >> "$display_error_tmp" ||
        ! printf '\n--- optional framebuffer attributes ---\n' >> "$display_error_tmp" ||
        ! calendar_display_geometry >> "$display_error_tmp" ||
        ! mv -f "$display_error_tmp" "$display_error_file"; then
        rm -f "$display_error_tmp"
        log 'DISPLAY_DIAGNOSTIC_FAILED: private diagnostic could not be published.'
        return 14
    fi
    log 'DISPLAY_DIAGNOSTIC_SAVED: private display-refresh-error.log retained; do not publish raw output.' || return 14
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
        if [ "$1" = "${FBINK-}" ] && [ "$display_stage" != capabilities ] && [ "$display_stage" != info ] &&
            [ -s "$CALENDAR_DISPLAY_OUTPUT" ]; then
            log "DISPLAY_FAILED: stage=$display_stage diagnostic output despite exit=0; output withheld." || return 14
            if [ "$display_stage" = refresh ] && ! calendar_display_save_failure 0 "$display_stage" "$*"; then
                log 'DISPLAY_DIAGNOSTIC_FAILED: original display failure retained; normal recovery still required.'
            fi
            return 15
        fi
        return 0
    else
        display_rc=$?
    fi
    log "DISPLAY_FAILED: stage=$display_stage exit=$display_rc; command output withheld." || return 14
    case "$display_rc" in
        16|32|129|130|143) return "$display_rc" ;;
    esac
    case "$display_stage" in
        refresh|info)
            if ! calendar_display_save_failure "$display_rc" "$display_stage" "$*"; then
                log 'DISPLAY_DIAGNOSTIC_FAILED: original display failure retained; normal recovery still required.'
            fi
            ;;
    esac
    return 15
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

calendar_display_parse_info() {
    display_info_size=$(wc -c < "$CALENDAR_DISPLAY_OUTPUT") || return 1
    [ "$display_info_size" -le 8192 ] || return 1
    display_info=$(awk '
        function assignment(    key,value,equal) {
            sub(/^[[:space:]]+/, "", token)
            sub(/[[:space:]]+$/, "", token)
            if (token == "") return
            equal=index(token, "=")
            if (!equal) { bad=1; return }
            key=substr(token, 1, equal-1)
            value=substr(token, equal+1)
            if (key !~ /^[A-Za-z_][A-Za-z0-9_]*$/ || seen[key]++) { bad=1; return }
            if (key in required) {
                if (value !~ /^(0|[1-9][0-9]*)$/ || length(value)>4 || value+0>8192) {
                    bad=1; return
                }
                number[key]=value+0
            }
        }
        BEGIN {
            split("screenWidth screenHeight viewWidth viewHeight viewHoriOrigin viewVertOrigin viewVertOffset currentRota FONTW FONTH FONTSIZE_MULT isKindleLegacy", keys, " ")
            for (i in keys) required[keys[i]]=1
            single=sprintf("%c",39); double=sprintf("%c",34)
        }
        { data=data $0 "\n" }
        END {
            for (i=1; i<=length(data); i++) {
                c=substr(data,i,1)
                if (quote != "") {
                    token=token c
                    if (escaped) escaped=0
                    else if (c=="\\" && quote==double) escaped=1
                    else if (c==quote) quote=""
                } else if (c==single || c==double) { quote=c; token=token c }
                else if (c==";") { assignment(); token="" }
                else token=token c
            }
            if (quote != "") bad=1
            assignment()
            for (key in required) if (!(key in number)) bad=1
            if (bad || number["screenWidth"]<1 || number["screenHeight"]<1 ||
                number["viewWidth"]!=number["screenWidth"] || number["viewHeight"]!=number["screenHeight"] ||
                number["viewHoriOrigin"]!=0 || number["viewVertOrigin"]!=0 || number["viewVertOffset"]!=0 ||
                number["currentRota"]>3 || number["FONTW"]!=8 || number["FONTH"]!=8 ||
                number["FONTSIZE_MULT"]!=1 || number["isKindleLegacy"]!=0) exit 1
            print number["screenWidth"], number["screenHeight"], number["currentRota"]
        }
    ' "$CALENDAR_DISPLAY_OUTPUT") || return 1
    read -r display_width display_height display_rotation <<EOF
$display_info
EOF
}

calendar_display_rect_valid() {
    [ "$1" -ge 0 ] && [ "$2" -ge "$display_bar_top" ] &&
        [ "$3" -gt 0 ] && [ "$4" -gt 0 ] &&
        [ "$(($1 + $3))" -le "$display_width" ] &&
        [ "$(($2 + $4))" -le "$display_height" ]
}

calendar_display_layout() {
    # The existing w=-1,h=-1 image path stretches to the entire -V viewport.
    display_bar_height=$((36 * display_height / 1448))
    display_bar_top=$((display_height - display_bar_height))
    for display_scale in 3 2; do
        display_font=$((8 * display_scale))
        [ "$display_bar_height" -ge "$((display_font + 2))" ] || continue
        if [ "$display_scale" = 3 ]; then
            display_outline_w=32 display_outline_h=18 display_border=2
            display_tip_w=3 display_tip_h=8 display_gap=10
            display_fill_max=25 display_fill_h=10 display_fill_offset=4
        else
            display_outline_w=22 display_outline_h=12 display_border=1
            display_tip_w=2 display_tip_h=6 display_gap=6
            display_fill_max=16 display_fill_h=6 display_fill_offset=3
        fi
        display_percent_x=$((display_width - 12 - 4 * display_font))
        display_tip_x=$((display_percent_x - display_gap - display_tip_w))
        display_outline_x=$((display_tip_x - 1 - display_outline_w))
        # Reserve all 24 timestamp cells, even when the current value is "--".
        [ "$((12 + 24 * display_font + 8))" -le "$display_outline_x" ] || continue
        display_text_y=$((display_bar_top + (display_bar_height - display_font) / 2))
        display_outline_y=$((display_bar_top + (display_bar_height - display_outline_h) / 2))
        display_tip_y=$((display_bar_top + (display_bar_height - display_tip_h) / 2))
        display_inner_x=$((display_outline_x + display_border))
        display_inner_y=$((display_outline_y + display_border))
        display_inner_w=$((display_outline_w - 2 * display_border))
        display_inner_h=$((display_outline_h - 2 * display_border))
        display_fill_x=$((display_outline_x + display_fill_offset))
        display_fill_y=$((display_outline_y + display_fill_offset))
        display_dead_x=$(((display_width % display_font) / 2))
        display_time_offset=$((12 - display_dead_x))
        display_percent_offset=$((display_percent_x - display_dead_x))
        display_region="top=$display_bar_top,left=0,width=$display_width,height=$display_bar_height"
        calendar_display_rect_valid 0 "$display_bar_top" "$display_width" "$display_bar_height" &&
            calendar_display_rect_valid 12 "$display_text_y" "$((24 * display_font))" "$display_font" &&
            calendar_display_rect_valid "$display_percent_x" "$display_text_y" "$((4 * display_font))" "$display_font" &&
            calendar_display_rect_valid "$display_outline_x" "$display_outline_y" "$display_outline_w" "$display_outline_h" &&
            calendar_display_rect_valid "$display_inner_x" "$display_inner_y" "$display_inner_w" "$display_inner_h" &&
            calendar_display_rect_valid "$display_tip_x" "$display_tip_y" "$display_tip_w" "$display_tip_h" &&
            calendar_display_rect_valid "$display_fill_x" "$display_fill_y" "$display_fill_max" "$display_fill_h" || continue
        return 0
    done
    log 'DISPLAY_GEOMETRY_FAILED: mapped status band cannot contain supported text and battery layout.'
    return 15
}

calendar_display_info() {
    display_width= display_height= display_rotation=
    calendar_display_command info 3 "$FBINK" -e -V -F IBM -S 1 || return "$?"
    if calendar_display_parse_info && calendar_display_layout; then return 0; fi
    log 'DISPLAY_INFO_FAILED: missing, unsafe or unsupported visible geometry; no image or status drawing attempted.'
    if ! calendar_display_save_failure 0 info "$FBINK -e -V -F IBM -S 1"; then
        log 'DISPLAY_DIAGNOSTIC_FAILED: geometry failure retained; normal recovery still required.'
    fi
    return 15
}

calendar_display_image() {
    calendar_display_require || return "$?"
    calendar_display_info || return "$?"
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
    display_fill=$((display_percent * display_fill_max / 100))
    display_label=$(printf '%3d%%' "$display_percent")
    calendar_display_ready || return "$?"
    calendar_display_command clear 3 "$FBINK" -q -b -B WHITE \
        -k "$display_region" || return "$?"
    calendar_display_command timestamp 3 "$FBINK" -q -b -V -F IBM -S "$display_scale" -C BLACK -B WHITE \
        -x 0 -y 0 -X "$display_time_offset" -Y "$display_text_y" "Updated $CALENDAR_STATUS_TIME" || return "$?"
    calendar_display_command outline 3 "$FBINK" -q -b -B BLACK \
        -k "top=$display_outline_y,left=$display_outline_x,width=$display_outline_w,height=$display_outline_h" || return "$?"
    calendar_display_command interior 3 "$FBINK" -q -b -B WHITE \
        -k "top=$display_inner_y,left=$display_inner_x,width=$display_inner_w,height=$display_inner_h" || return "$?"
    calendar_display_command terminal 3 "$FBINK" -q -b -B BLACK \
        -k "top=$display_tip_y,left=$display_tip_x,width=$display_tip_w,height=$display_tip_h" || return "$?"
    # A zero-sized cls region may mean full screen, never an empty fill.
    if [ "$display_fill" -gt 0 ]; then
        calendar_display_command fill 3 "$FBINK" -q -b -B BLACK \
            -k "top=$display_fill_y,left=$display_fill_x,width=$display_fill,height=$display_fill_h" || return "$?"
    fi
    calendar_display_command percent 3 "$FBINK" -q -b -V -F IBM -S "$display_scale" -C BLACK -B WHITE \
        -x 0 -y 0 -X "$display_percent_offset" -Y "$display_text_y" "$display_label" || return "$?"
    calendar_display_command refresh 5 "$FBINK" -q -w -W GC16 \
        -s "$display_region"
}
