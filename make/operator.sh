#!/usr/bin/env bash
# Copied from chefnmr2-repro/make/operator.sh; only Docker context selection is needed.

choose() {
    local name=$1 choices=${2:-} input="WHEELHOUSE_$1" value option number valid
    local -a options=()
    # Discovery commands may return one choice per line.
    read -ra options <<< "${choices//$'\n'/ }"
    if [[ " $WHEELHOUSE_EXPLICIT_INPUTS " == *" $name "* ]]; then
        value=${!input}
    else
        if ! { exec 3<>/dev/tty; } 2>/dev/null; then
            printf 'Supply %s=<value> on the make command line; a terminal is required to ask for it.\n' "$name" >&2
            return 2
        fi
        for number in "${!options[@]}"; do
            printf '  %s) %s\n' "$((number+1))" "${options[number]}" >&3
        done
        printf 'Select %s (Enter cancels) > ' "$name" >&3
        IFS= read -r value <&3 || value=
        exec 3>&-
        for number in "${!options[@]}"; do
            if [[ $value == "$((number+1))" ]]; then
                value=${options[number]}
                break
            fi
        done
    fi
    if [[ -z $value ]]; then
        printf '%s selection cancelled.\n' "$name" >&2
        return 2
    fi
    if [[ -n $choices ]]; then
        valid=
        for option in "${options[@]}"; do
            if [[ $value == "$option" ]]; then valid=yes; break; fi
        done
        if [[ -z $valid ]]; then
            printf 'Invalid %s=%s. Choose from %s.\n' "$name" "$value" "${options[*]}" >&2
            return 2
        fi
    fi
    printf '%s\n' "$value"
}

select_docker() {
    local variable context endpoint contexts=
    for variable in ${!DOCKER_@} ${!COMPOSE_@} ${!BUILDX_@} ${!BUILDKIT_@}; do
        unset "$variable"
    done
    if [[ " $WHEELHOUSE_EXPLICIT_INPUTS " != *' DOCKER_CONTEXT '* ]]; then
        contexts=$(docker context ls --format '{{.Name}}')
    fi
    context=$(choose DOCKER_CONTEXT "$contexts")
    endpoint=$(docker context inspect "$context" --format '{{.Endpoints.docker.Host}}')
    case $endpoint in
        unix:///*) ;;
        *) printf 'Docker context %s must use a local Unix socket.\n' "$context" >&2; return 2 ;;
    esac
    printf 'Using Docker context %s (%s)\n' "$context" "$endpoint" >&2
    for variable in ${!WHEELHOUSE_@}; do unset "$variable"; done
    unset MAKEFLAGS MFLAGS GNUMAKEFLAGS MAKEOVERRIDES
    docker=(docker --host "$endpoint")
    docker_host=$endpoint
}
