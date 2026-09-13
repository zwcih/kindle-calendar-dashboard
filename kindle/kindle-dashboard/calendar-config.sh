#!/bin/sh

# Library code only. The private configuration is data, never shell code.
calendar_load_config() {
    IMAGE_URL=
    WIFI_SSID=
    IMAGE_MODE=static
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
            } else if (key == "IMAGE_MODE") {
                if (value != "static" && value != "dynamic") invalid()
                mode=value
            } else invalid()
        }
        END {
            if (bad || !seen["IMAGE_URL"] || !seen["WIFI_SSID"]) exit 1
            if (mode == "dynamic" && url ~ /["#]/) exit 1
            print mode == "" ? "static" : mode
            print url
            print ssid
        }
    ' "$1") || {
        printf 'CONFIG_ERROR: require LF HTTPS IMAGE_URL, WIFI_SSID (1..32 bytes), optional IMAGE_MODE=static|dynamic; dynamic URL forbids double quotes and fragments.\n' >&2
        return 10
    }
    IMAGE_MODE=${calendar_config_data%%'
'*}
    calendar_config_data=${calendar_config_data#*'
'}
    IMAGE_URL=${calendar_config_data%%'
'*}
    WIFI_SSID=${calendar_config_data#*'
'}
    unset calendar_config_data calendar_config_size
}

# The token stays inside awk: never a shell variable, environment value or argv.
calendar_auth_header() {
    if [ ! -f "$1" ] || [ -L "$1" ] || [ ! -r "$1" ]; then
        printf 'AUTH_CONFIG_ERROR: readable regular image-auth.local.conf required.\n' >&2
        return 10
    fi
    calendar_auth_size=$(wc -c < "$1") || return 10
    if [ "$calendar_auth_size" -gt 4096 ]; then
        printf 'AUTH_CONFIG_ERROR: credential data exceeds 4096 bytes.\n' >&2
        return 10
    fi
    LC_ALL=C awk -v BINMODE=1 '
        {
            if (NR != 1 || /[[:cntrl:]]/ || index($0, "BEARER_TOKEN=") != 1) {
                bad=1; exit 1
            }
            token=substr($0, 14)
            if (length(token) < 1 || token !~ /^[A-Za-z0-9._~+\/-]+=*$/) {
                bad=1; exit 1
            }
        }
        END {
            if (bad || NR != 1) exit 1
            printf "header = \"Authorization: Bearer %s\"\n", token
        }
    ' "$1" || {
        printf 'AUTH_CONFIG_ERROR: expected one LF BEARER_TOKEN line with a valid bearer value.\n' >&2
        return 10
    }
}

calendar_validate_auth() {
    [ "$IMAGE_MODE" = dynamic ] || return 0
    calendar_auth_header "$1" >/dev/null
}

calendar_write_request() {
    calendar_load_config "$1" || return 10
    [ "$IMAGE_MODE" = dynamic ] || return 10
    case "$4" in 0|[1-9]|[1-9][0-9]|100) ;; *) return 31 ;; esac
    case "$5" in 0) calendar_boolean=false ;; 1) calendar_boolean=true ;; *) return 31 ;; esac
    # Quoting is safe: URL disallows quote/backslash/control; token is RFC 6750 data.
    {
        printf 'url = "%s"\n' "$IMAGE_URL" &&
            calendar_auth_header "$2" &&
            printf 'data = "{\\"battery\\":%s,\\"charging\\":%s}"\n' "$4" "$calendar_boolean"
    } > "$3"
}
