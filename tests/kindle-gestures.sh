#!/bin/sh
# Invoked only by isolated host fixtures after extracting the actual functions.
set -eu
RUN=.
record() { :; }
alive() { return 1; }
run() { :; }
touch_owner=999999
touch_owner_start=1

event() {
    value=$5
    if [ "$value" = -1 ]; then
        gesture_event "$(($1 % 65536))" "$(($1 / 65536))" "$(($2 % 65536))" "$(($2 / 65536))" "$3" "$4" 65535 65535
    else
        gesture_event "$(($1 % 65536))" "$(($1 / 65536))" "$(($2 % 65536))" "$(($2 / 65536))" "$3" "$4" "$value" 0
    fi
}
reset_gesture() {
    rm -f manual-refresh stop-requested gesture-held reached-ordinary-hold
    pressed=0 current_slot=0 tracked_slot=0 mt_seen=0 source= multiple=0
    contacts= last_sec=
}
no_action() {
    [ ! -f manual-refresh ] && [ ! -f stop-requested ]
}

if [ "${EXPECT_OLD_BUG:-0}" = 1 ]; then
    for duration in 1 3; do
        (
            reset_gesture
            event 100 0 3 57 10
            event 100 0 3 47 1
            event 100 0 3 57 11
            event 100 0 3 47 0
            event 100 1 3 57 -1
            event 101 0 3 57 12
            event "$((101 + duration))" 0 3 57 -1
        )
        if [ "$duration" = 1 ]; then [ -f manual-refresh ]
        else [ -f stop-requested ]; fi
        printf 'REPRODUCED old multi-touch false action duration=%s\n' "$duration"
    done
    exit 0
fi

# Fork each case: a valid long release deliberately exits the touch process.
for order in first second; do
    for duration in 1 3; do
        (
            reset_gesture
            event 100 0 1 330 1
            event 100 0 3 57 10
            event 100 0 3 57 10
            event 100 0 3 47 1
            event 100 0 3 57 11
            if [ "$order" = first ]; then event 100 0 3 47 0; fi
            event 100 100 3 57 -1
            event 100 100 1 330 0
            [ -f gesture-held ]
            event 101 0 3 57 12
            event "$((101 + duration))" 0 3 57 -1
            no_action
            [ -f gesture-held ]
            if [ "$order" = first ]; then event 105 0 3 47 1
            else event 105 0 3 47 0; fi
            event 105 0 3 57 -1
            no_action
            [ ! -f gesture-held ]
            event 106 0 3 57 13
            event 106 1 3 57 -1
            [ -f manual-refresh ]
            rm -f manual-refresh
            : > reached-ordinary-hold
            event 107 0 3 57 14
            event 109 0 3 57 -1
        )
        # A false long release must not pass by exiting the subshell early.
        [ -f reached-ordinary-hold ]
        [ -f stop-requested ] && [ ! -f manual-refresh ]
        printf 'PASS multi-contact %s duration=%s\n' "$order" "$duration"
    done
done

for micros in 899999 900000; do
    (
        reset_gesture
        event 100 900000 3 57 20
        event 102 "$micros" 3 57 -1
    )
    if [ "$micros" = 899999 ]; then [ -f manual-refresh ] && [ ! -f stop-requested ]
    else [ -f stop-requested ] && [ ! -f manual-refresh ]; fi
done
(
    reset_gesture
    event 100 0 3 57 -1
    event 100 0 1 330 0
    no_action
    event 100 500 3 57 21
    event 100 499 3 53 1
    event 101 0 3 57 -1
    no_action
    event 102 0 3 57 22
    event 102 1 3 57 -1
    [ -f manual-refresh ]
)
(
    reset_gesture
    event 100 0 1 330 1
    event 100 1 1 330 0
    [ -f manual-refresh ]
)
for button_order in before after; do
    (
        reset_gesture
        if [ "$button_order" = before ]; then event 100 0 1 330 1; fi
        event 100 0 3 57 23
        if [ "$button_order" = after ]; then event 100 0 1 330 1; fi
        event 100 1 1 330 0
        no_action
        [ -f gesture-held ]
        event 100 2 3 57 -1
        [ -f manual-refresh ] && [ ! -f gesture-held ]
        rm -f manual-refresh
        event 100 2 3 57 -1
        event 100 2 1 330 0
        no_action
    )
done
(
    reset_gesture
    event 100 0 3 57 27
    event 100 1 3 57 28
    event 101 0 3 57 -1
    no_action
    [ ! -f gesture-held ]
)
(
    reset_gesture
    event 100 0 3 57 25
    event 100 0 3 47 1
    event 100 0 3 57 26
    event 100 0 3 47 0
    event 100 1 3 57 -1
    gesture_window
)
[ -f stop-requested ]
set +e
( reset_gesture; event 100 0 0 3 0 )
rc=$?
set -e
[ "$rc" = 16 ]
set +e
( reset_gesture; gesture_event 100 0 0 0 3 57 '1+1' 0 )
rc=$?
set -e
[ "$rc" = 16 ]
printf 'PASS boundaries, wake releases, backwards time, BTN, timeout and SYN_DROPPED\n'
