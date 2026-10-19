#!/bin/sh
# Remove a Lynewright stack after explicit confirmation.
set -eu

fail() {
    printf 'Lynewright uninstall: %s\n' "$1" >&2
    exit 1
}

shell_quote() {
    printf "'"
    printf '%s' "$1" | sed "s/'/'\\\\''/g"
    printf "'"
}

yes=0
case "$#" in
    0) ;;
    1)
        case "$1" in
            --yes) yes=1 ;;
            --help|-h)
                printf '%s\n' 'Usage: sh uninstall.sh [--yes]' '--yes removes the stack, persistent data volume, and this checkout without prompting.'
                exit 0
                ;;
            *) fail "unknown option: $1" ;;
        esac
        ;;
    *) fail 'accepts only --yes' ;;
esac

script_dir=$(CDPATH='' cd "$(dirname "$0")" && pwd -P)
home_dir=$(CDPATH='' cd "$HOME" && pwd -P)
case "$script_dir" in
    /) fail 'refusing to remove the filesystem root.' ;;
esac
case "$home_dir/" in
    "$script_dir/"*) fail 'refusing to remove the home directory or one of its parent directories.' ;;
esac
[ -d "$script_dir/.git" ] || [ -f "$script_dir/.git" ] || fail 'this directory is not a Git checkout.'
if [ ! -f "$script_dir/README.md" ] || ! grep -qx '# Lynewright' "$script_dir/README.md"; then
    fail 'this directory is not a Lynewright checkout.'
fi
if [ ! -f "$script_dir/docker-compose.yml" ] || [ ! -f "$script_dir/backend/app/main.py" ]; then
    fail 'Lynewright application files are missing; no stack was changed.'
fi

compose_driver=
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    compose_driver=docker
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    compose_driver=podman
elif command -v podman-compose >/dev/null 2>&1; then
    compose_driver=podman-compose
else
    fail 'Docker Compose, Podman Compose, or podman-compose is required to stop the stack.'
fi

compose() {
    case "$compose_driver" in
        docker) docker compose "$@" ;;
        podman) podman compose "$@" ;;
        podman-compose) podman-compose "$@" ;;
        *) fail 'no supported compose command was selected.' ;;
    esac
}

cd "$script_dir"
compose down --remove-orphans

ask() {
    printf '%s [y/N] ' "$1"
    answer=
    IFS= read -r answer || answer=
    case "$answer" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

remove_volume=0
remove_directory=0
if [ "$yes" -eq 1 ]; then
    remove_volume=1
    remove_directory=1
else
    if ask 'Delete the persistent Lynewright data volume?'; then
        remove_volume=1
    fi
    if ask "Delete the checkout at $script_dir?"; then
        remove_directory=1
    fi
fi

if [ "$remove_directory" -eq 1 ]; then
    packages_dir="$script_dir/data/packages"
    quoted_packages_dir=$(shell_quote "$packages_dir")
    if ! compose run --rm --no-deps --entrypoint sh api -c 'rm -rf /data/packages/* /data/packages/.[!.]* /data/packages/..?*'; then
        fail "could not clear the package bind directory from the API container; to remove it manually, run: sudo rm -rf $quoted_packages_dir"
    fi
fi

if [ "$remove_volume" -eq 1 ]; then
    compose down --volumes --remove-orphans --rmi local
else
    printf 'Persistent data volume kept.\n'
fi

if [ "$remove_directory" -eq 1 ]; then
    parent_dir=$(dirname "$script_dir")
    cd "$parent_dir"
    quoted_script_dir=$(shell_quote "$script_dir")
    if ! rm -rf "$script_dir"; then
        fail "could not remove the checkout; to remove it manually, run: sudo rm -rf $quoted_script_dir"
    fi
    printf 'Removed the Lynewright checkout.\n'
else
    printf 'Checkout kept at %s.\n' "$script_dir"
fi
