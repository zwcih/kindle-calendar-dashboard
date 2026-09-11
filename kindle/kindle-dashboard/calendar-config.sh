#!/bin/sh

# Library code only. The private configuration is data, never shell code.
calendar_load_config() {
    IMAGE_URL=
    WIFI_SSID=
    if [ ! -f "$1" ] || [ -L "$1" ] || [ ! -r "$1" ]; then
        printf 'CONFIG_ERROR: readable regular config.local.conf required; see kindle/README.md.\n' >&2
        return 10
    fi
    calendar_config_size=$(wc -c < "$1") || return 10
    if [ "$calendar_config_size" -gt 8192 ]; then
        printf 'CONFIG_ERROR: configuration exceeds 8192 bytes.\n' >&2
        return 10
    fi
    calendar_config_data=$(LC_ALL=C awk -v BINMODE=1 '
        function invalid() { bad=1; exit 1 }
        /^[[:space:]]*$/ { next }
        /^#/ { next }
        {
            if (/[[:cntrl:]]/) invalid()
            separator=index($0, "=")
            if (!separator) invalid()
            key=substr($0, 1, separator-1)
            value=substr($0, separator+1)
            if (seen[key]++) invalid()
            if (key == "IMAGE_URL") {
                if (value !~ /^https:\/\// || value ~ /[[:space:]\\]/) invalid()
                authority=substr(value, 9)
                sub(/[\/?#].*$/, "", authority)
                if (authority !~ /^[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]+)?$/) invalid()
                parts=split(authority, host, ":")
                if (host[1] ~ /[.]$/ || host[1] ~ /[.][.]/) invalid()
                if (parts == 2 && (host[2]+0 < 1 || host[2]+0 > 65535)) invalid()
                url=value
            } else if (key == "WIFI_SSID") {
                if (length(value) < 1 || length(value) > 32) invalid()
                ssid=value
            } else invalid()
        }
        END {
            if (bad || !seen["IMAGE_URL"] || !seen["WIFI_SSID"]) exit 1
            print url
            print ssid
        }
    ' "$1") || {
        printf 'CONFIG_ERROR: use LF KEY=value data with one valid HTTPS IMAGE_URL and one WIFI_SSID (1..32 bytes); no quotes or shell assignments.\n' >&2
        return 10
    }
    IMAGE_URL=${calendar_config_data%%'
'*}
    WIFI_SSID=${calendar_config_data#*'
'}
    unset calendar_config_data calendar_config_size
}
