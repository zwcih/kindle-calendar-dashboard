#!/bin/sh
# Run from the repository root. stdout is a standalone Linux regression bundle.
set -eu
controller=kindle/kindle-dashboard/calendar-dedicated.sh
worker=kindle/kindle-dashboard/calendar-auto-refresh.sh
extract_function() {
    awk -v name="$2" '
        $0 == name "() {" { found=1; copying=1 }
        copying { print }
        copying && $0 == "}" { copying=0 }
        END { if (!found || copying) exit 1 }
    ' "$1"
}
extract_between() {
    awk -v first="$2" -v last="$3" -v relocate="${4:-0}" '
        $0 == last && copying { finished=1; exit }
        copying {
            if (relocate) sub(/TRIAL" = \/tmp\/calendar-dedicated/, "TRIAL\" = \"$RUN\"")
            print
        }
        $0 == first { copying=1; found=1 }
        END { if (!found || !finished) exit 1 }
    ' "$1"
}
asset() {
    printf "cat > \"\$FIXTURE/%s\" <<'KINDLE_FIXTURE_ASSET'\n" "$1"
}
end_asset() { printf '\nKINDLE_FIXTURE_ASSET\n'; }
cat tests/kindle-lock-bootstrap.sh
asset lock.sh
cat kindle/kindle-dashboard/calendar-lock.sh
end_asset
asset config.sh
cat kindle/kindle-dashboard/calendar-config.sh
end_asset
asset actual.sh
for name in identity alive signal_exit request_stop lease guard_ok honor_signal cancel_child run \
    read_number stop_touch restore; do
    extract_function "$controller" "$name"
done
end_asset
asset worker-actual.sh
for name in identity dedicated_owner_alive; do extract_function "$worker" "$name"; done
end_asset
asset worker-cleanup.sh
for name in same_child cancel_child cleanup; do extract_function "$worker" "$name"; done
end_asset
asset controller-cleanup.sh
extract_function "$controller" cleanup
end_asset
asset worker-entry.sh
# Only relocate the dedicated runtime comparison, never change acquisition logic.
extract_between "$worker" 'launch_guard=1' 'honor_signal' 1
end_asset
asset start-entry.sh
printf 'case "${1-}" in\n    --start)\n'
extract_between "$controller" '    --start)' '    --stop|--recover)'
printf 'esac\n'
end_asset
asset stop-entry.sh
printf 'case "${1-}" in\n    --stop|--recover)\n'
extract_between "$controller" '    --stop|--recover)' '    --touch)'
printf 'esac\n'
end_asset
asset guard-entry.sh
printf 'case "${1-}" in\n    --guard)\n'
extract_between "$controller" '    --guard)' '    --run) ;;'
printf 'esac\n'
end_asset
asset owner-entry.sh
printf 'identity "$$" || exit 12\n'
extract_between "$controller" 'identity "$$" || exit 12' ': > "$RUN/sleep-owned" || exit 14'
end_asset
asset preflight-lock-entry.sh
# Stop before device/global-path checks; run the real acquisition and FD close.
extract_between "$controller" 'preflight() {' '    for other in /tmp/calendar-legacy-auto /tmp/calendar-sleep-wake-test \'
end_asset
for file in common actor controller scenarios; do
    asset "$file.sh"
    cat "tests/kindle-lock-$file.sh"
    end_asset
done
cat <<'KINDLE_FIXTURE_RUN'
. "$FIXTURE/common.sh"
register
save_identity suite
/bin/sh "$FIXTURE/actor.sh" suite-watchdog >&2 &
. "$FIXTURE/scenarios.sh"
KINDLE_FIXTURE_RUN
