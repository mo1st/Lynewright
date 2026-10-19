#!/bin/sh
# Lynewright installer.
#
# Options:
#   LYNEWRIGHT_DEMO (default 1)
#   LYNEWRIGHT_DIR, LYNEWRIGHT_REF, LYNEWRIGHT_PORT, LYNEWRIGHT_BIND
#   LYNEWRIGHT_ADMIN_EMAIL, LYNEWRIGHT_REPO_URL
#   LYNEWRIGHT_ALLOW_ROOT=1 to allow a root install
#   --no-start prepares the checkout and .env without starting containers
set -eu

usage() {
    printf '%s\n' \
        'Usage: sh install.sh [--no-start]' \
        '' \
        'Installs or upgrades Lynewright, creates .env if absent, and starts the stack.' \
        'Set LYNEWRIGHT_DIR, LYNEWRIGHT_REF, LYNEWRIGHT_PORT, LYNEWRIGHT_BIND,' \
        'LYNEWRIGHT_ADMIN_EMAIL, or LYNEWRIGHT_REPO_URL to customize the install.'
}

fail() {
    printf 'Lynewright install: %s\n' "$1" >&2
    exit 1
}

no_start=0
case "$#" in
    0) ;;
    1)
        case "$1" in
            --no-start) no_start=1 ;;
            --help|-h) usage; exit 0 ;;
            *) usage >&2; fail "unknown option: $1" ;;
        esac
        ;;
    *) usage >&2; fail 'accepts only --no-start' ;;
esac

system_name=$(uname -s 2>/dev/null || printf 'unknown')
case "$system_name" in
    Windows_NT*|MINGW*|MSYS*|CYGWIN*)
        fail 'Windows is not supported directly. Run this script inside WSL.'
        ;;
esac

command -v id >/dev/null 2>&1 || fail 'id is required to set the API data owner.'
install_uid=$(id -u)
install_gid=$(id -g)
case "$install_uid" in
    ''|*[!0123456789]*) fail 'could not read numeric installer uid.' ;;
esac
case "$install_gid" in
    ''|*[!0123456789]*) fail 'could not read numeric installer gid.' ;;
esac
if [ "$install_uid" -eq 0 ] && [ "${LYNEWRIGHT_ALLOW_ROOT:-0}" != 1 ]; then
    fail 'refusing to install as root; use a regular account or set LYNEWRIGHT_ALLOW_ROOT=1.'
fi

command -v git >/dev/null 2>&1 || fail 'git is required. Install Git, then run this command again.'
home=${HOME:-}
[ -n "$home" ] || fail 'HOME is not set.'

if [ "$no_start" -eq 0 ] && ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    fail 'curl or wget is required to check that the dashboard is ready.'
fi

repo_url=${LYNEWRIGHT_REPO_URL:-https://github.com/mo1st/Lynewright.git}
ref=${LYNEWRIGHT_REF:-main}
install_dir=${LYNEWRIGHT_DIR:-"$home/lynewright"}
case "$install_dir" in
    ''|/|.|..|"$home")
        fail 'LYNEWRIGHT_DIR must name a dedicated install directory.'
        ;;
esac

is_commit_id() {
    value=$1
    case "$value" in
        ''|*[!0123456789abcdefABCDEF]*) return 1 ;;
    esac
    [ "${#value}" -eq 40 ] || [ "${#value}" -eq 64 ]
}

is_lynewright_checkout() {
    candidate=$1
    [ -d "$candidate" ] || return 1
    { [ -d "$candidate/.git" ] || [ -f "$candidate/.git" ]; } || return 1
    [ "$(git -C "$candidate" rev-parse --is-inside-work-tree 2>/dev/null || true)" = true ] || return 1
    [ -f "$candidate/README.md" ] || return 1
    grep -qx '# Lynewright' "$candidate/README.md" || return 1
    [ -f "$candidate/docker-compose.yml" ] && [ -f "$candidate/backend/app/main.py" ]
}

parent_dir=$(dirname "$install_dir")
mkdir -p "$parent_dir"
if [ -d "$install_dir" ] || [ -f "$install_dir" ] || [ -L "$install_dir" ]; then
    is_lynewright_checkout "$install_dir" || fail "refusing to overwrite existing path: $install_dir"
    printf 'Updating Lynewright in %s\n' "$install_dir"
    current_branch=$(git -C "$install_dir" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    if [ "$ref" = main ] && [ "$current_branch" = main ]; then
        git -C "$install_dir" pull --ff-only
    else
        git -C "$install_dir" fetch --depth 1 origin "$ref"
        git -C "$install_dir" checkout --detach FETCH_HEAD
    fi
else
    printf 'Downloading Lynewright from %s\n' "$repo_url"
    if is_commit_id "$ref"; then
        git clone --depth 1 "$repo_url" "$install_dir"
        git -C "$install_dir" fetch --depth 1 origin "$ref"
        git -C "$install_dir" checkout --detach FETCH_HEAD
    else
        git clone --depth 1 --branch "$ref" "$repo_url" "$install_dir"
    fi
fi

is_lynewright_checkout "$install_dir" || fail 'the downloaded repository does not have the expected Lynewright files.'
[ -f "$install_dir/.env.example" ] || fail 'the checkout is missing .env.example.'

# Create bind-mount sources as the installing user before Compose can create them as root.
mkdir -p "$install_dir/data/packages"

compose_driver=
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    compose_driver=docker
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    compose_driver=podman
elif command -v podman-compose >/dev/null 2>&1; then
    compose_driver=podman-compose
else
    fail 'Docker Compose, Podman Compose, or podman-compose is required. Install one, then retry.'
fi

compose() (
    cd "$install_dir"
    case "$compose_driver" in
        docker) docker compose "$@" ;;
        podman) podman compose "$@" ;;
        podman-compose) podman-compose "$@" ;;
        *) fail 'no supported compose command was selected.' ;;
    esac
)

requested_port=${LYNEWRIGHT_PORT:-8080}
requested_bind=${LYNEWRIGHT_BIND:-127.0.0.1}
demo=${LYNEWRIGHT_DEMO:-1}
case "$requested_port" in
    ''|*[!0123456789]*) fail 'LYNEWRIGHT_PORT must be a number from 1 to 65535.' ;;
esac
if [ "$requested_port" -lt 1 ] || [ "$requested_port" -gt 65535 ]; then
    fail 'LYNEWRIGHT_PORT must be a number from 1 to 65535.'
fi
case "$requested_bind" in
    127.0.0.1|0.0.0.0) ;;
    *) fail 'LYNEWRIGHT_BIND must be 127.0.0.1 or 0.0.0.0.' ;;
esac
case "$demo" in
    0|1) ;;
    *) fail 'LYNEWRIGHT_DEMO must be 0 or 1.' ;;
esac

random_hex() {
    byte_count=$1
    generated=
    if command -v openssl >/dev/null 2>&1; then
        generated=$(openssl rand -hex "$byte_count" 2>/dev/null || true)
    fi
    if [ "${#generated}" -ne $((byte_count * 2)) ] && [ -r /dev/urandom ]; then
        generated=$(od -An -N"$byte_count" -tx1 /dev/urandom | tr -d ' 
')
    fi
    [ "${#generated}" -eq $((byte_count * 2)) ] || return 1
    case "$generated" in
        *[!0123456789abcdefABCDEF]*) return 1 ;;
    esac
    printf '%s
' "$generated"
}

random_password() {
    random_hex 12
}

generated_password=
if [ -L "$install_dir/.env" ]; then
    fail 'refusing a symbolic .env path.'
elif [ ! -e "$install_dir/.env" ]; then
    admin_email=${LYNEWRIGHT_ADMIN_EMAIL:-admin@example.com}
    admin_email=$(printf '%s' "$admin_email" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')
    printf '%s\n' "$admin_email" | LC_ALL=C grep -Eq '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$' || \
        fail 'LYNEWRIGHT_ADMIN_EMAIL must be a valid email address accepted by Lynewright sign-in.'
    generated_password=$(random_password) || fail 'could not generate a secure bootstrap password.'

    umask 077
    env_tmp=$install_dir/.env.tmp.$$
    LYNEWRIGHT_INSTALL_EMAIL=$admin_email awk -v password="$generated_password" -v port="$requested_port" -v bind="$requested_bind" -v demo="$demo" -v uid="$install_uid" -v gid="$install_gid" '
        function quote_env(value, quote, result, i, character) {
            quote = sprintf("%c", 39)
            result = quote
            for (i = 1; i <= length(value); i++) {
                character = substr(value, i, 1)
                if (character == quote) result = result "\\" quote
                else result = result character
            }
            return result quote
        }
        BEGIN {
            email = ENVIRON["LYNEWRIGHT_INSTALL_EMAIL"]
            email_done = 0
            password_done = 0
            bind_done = 0
            port_done = 0
            demo_done = 0
            uid_done = 0
            gid_done = 0
        }
        /^LYNEWRIGHT_BOOTSTRAP_EMAIL=/ {
            print "LYNEWRIGHT_BOOTSTRAP_EMAIL=" quote_env(email)
            email_done = 1
            next
        }
        /^LYNEWRIGHT_BOOTSTRAP_PASSWORD=/ {
            print "LYNEWRIGHT_BOOTSTRAP_PASSWORD=" password
            password_done = 1
            next
        }
        /^LYNEWRIGHT_BIND=/ {
            print "LYNEWRIGHT_BIND=" bind
            bind_done = 1
            next
        }
        /^LYNEWRIGHT_PORT=/ {
            print "LYNEWRIGHT_PORT=" port
            port_done = 1
            next
        }
        /^LYNEWRIGHT_DEMO=/ {
            print "LYNEWRIGHT_DEMO=" demo
            demo_done = 1
            next
        }
        /^LYNEWRIGHT_UID=/ {
            print "LYNEWRIGHT_UID=" uid
            uid_done = 1
            next
        }
        /^LYNEWRIGHT_GID=/ {
            print "LYNEWRIGHT_GID=" gid
            gid_done = 1
            next
        }
        { print }
        END {
            if (!email_done) print "LYNEWRIGHT_BOOTSTRAP_EMAIL=" quote_env(email)
            if (!password_done) print "LYNEWRIGHT_BOOTSTRAP_PASSWORD=" password
            if (!bind_done) print "LYNEWRIGHT_BIND=" bind
            if (!port_done) print "LYNEWRIGHT_PORT=" port
            if (!demo_done) print "LYNEWRIGHT_DEMO=" demo
            if (!uid_done) print "LYNEWRIGHT_UID=" uid
            if (!gid_done) print "LYNEWRIGHT_GID=" gid
        }
    ' "$install_dir/.env.example" > "$env_tmp"
    chmod 600 "$env_tmp"
    mv "$env_tmp" "$install_dir/.env"
    printf 'Created a private .env file.\n'
elif [ -f "$install_dir/.env" ]; then
    printf 'Keeping existing .env settings.\n'
else
    fail '.env exists but is not a regular file.'
fi

write_env_value() {
    env_name=$1
    env_value=$2
    env_tmp=$install_dir/.env.owner.$$
    umask 077
    LYNEWRIGHT_ENV_NAME=$env_name LYNEWRIGHT_ENV_VALUE=$env_value awk '
        BEGIN {
            name = ENVIRON["LYNEWRIGHT_ENV_NAME"]
            value = ENVIRON["LYNEWRIGHT_ENV_VALUE"]
            found = 0
        }
        index($0, name "=") == 1 {
            print name "=" value
            found = 1
            next
        }
        { print }
        END {
            if (!found) print name "=" value
        }
    ' "$install_dir/.env" > "$env_tmp"
    chmod 600 "$env_tmp"
    mv "$env_tmp" "$install_dir/.env"
}
write_env_value LYNEWRIGHT_UID "$install_uid"
write_env_value LYNEWRIGHT_GID "$install_gid"
read_env_value() {
    env_name=$1
    env_default=$2
    env_file=$3
    found=$(awk -F= -v name="$env_name" '
        $1 == name { value = substr($0, length(name) + 2) }
        END {
            quote = sprintf("%c", 39)
            if (substr(value, 1, 1) == quote && substr(value, length(value), 1) == quote) {
                raw = substr(value, 2, length(value) - 2)
                value = ""
                for (i = 1; i <= length(raw); i++) {
                    character = substr(raw, i, 1)
                    if (character == "\\" && i < length(raw) && substr(raw, i + 1, 1) == quote) {
                        value = value quote
                        i++
                    } else {
                        value = value character
                    }
                }
            }
            print value
        }
    ' "$env_file")
    if [ -n "$found" ]; then
        printf '%s\n' "$found"
    else
        printf '%s\n' "$env_default"
    fi
}

# Stored credentials are encrypted with this key. Older installs never had
# one; the API re-encrypts their secrets on the next start.
if [ -z "$(read_env_value LYNEWRIGHT_SECRET_KEY '' "$install_dir/.env")" ]; then
    secret_key=$(random_hex 32) || fail 'could not generate an encryption key for stored secrets.'
    write_env_value LYNEWRIGHT_SECRET_KEY "$secret_key"
    printf 'Generated a private encryption key for stored secrets.
'
fi

port=${LYNEWRIGHT_PORT:-$(read_env_value LYNEWRIGHT_PORT 8080 "$install_dir/.env")}
bind=${LYNEWRIGHT_BIND:-$(read_env_value LYNEWRIGHT_BIND 127.0.0.1 "$install_dir/.env")}
demo=${LYNEWRIGHT_DEMO:-$(read_env_value LYNEWRIGHT_DEMO 1 "$install_dir/.env")}
case "$port" in
    ''|*[!0123456789]*) fail 'LYNEWRIGHT_PORT must be a number from 1 to 65535.' ;;
esac
# Telegram is on for new installs (nothing contacts Telegram until a bot is
# connected in Settings). An existing install keeps its own choice.
if [ -n "$generated_password" ]; then
    write_env_value LYNEWRIGHT_TELEGRAM_ENABLED 1
fi
# Media links are absolute URLs built from LYNEWRIGHT_PUBLIC_URL, so it must
# name the port this install serves on. A fresh install always gets the local
# address, and a local address follows the port; a custom value (for example
# an HTTPS domain) is kept.
current_public_url=$(read_env_value LYNEWRIGHT_PUBLIC_URL '' "$install_dir/.env")
case "$current_public_url" in
    ''|http://localhost:[0-9]*|http://127.0.0.1:[0-9]*) public_url_is_local=1 ;;
    *) public_url_is_local=0 ;;
esac
if [ -n "$generated_password" ] || [ "$public_url_is_local" -eq 1 ]; then
    write_env_value LYNEWRIGHT_PUBLIC_URL "http://localhost:$port"
fi
case "$demo" in
    0|1) ;;
    *) fail 'LYNEWRIGHT_DEMO must be 0 or 1.' ;;
esac

if [ "$bind" = 0.0.0.0 ]; then
    printf '%s\n' 'Warning: this exposes Lynewright on every network interface. Put a TLS proxy in front before allowing other people to connect.' >&2
fi

if [ "$no_start" -eq 0 ]; then
    printf 'Starting Lynewright on port %s...\n' "$port"
    LYNEWRIGHT_PORT=$port LYNEWRIGHT_BIND=$bind LYNEWRIGHT_DEMO=$demo LYNEWRIGHT_UID=$install_uid LYNEWRIGHT_GID=$install_gid compose up -d --build --force-recreate
    printf 'Waiting for the health endpoint (up to 180 seconds)...\n'
    healthy=0
    attempt=0
    while [ "$attempt" -lt 180 ]; do
        if command -v curl >/dev/null 2>&1; then
            if curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
                healthy=1
                break
            fi
        elif wget -q -O /dev/null "http://127.0.0.1:$port/health"; then
            healthy=1
            break
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    [ "$healthy" -eq 1 ] || fail "the stack did not become healthy. Check compose logs in $install_dir."
    printf 'Lynewright is healthy.\n'
else
    printf 'Prepared the checkout and configuration; containers were not started.\n'
fi

printf '\nLynewright is available at http://127.0.0.1:%s\n' "$port"
printf 'MCP connection URL: http://127.0.0.1:%s/mcp\n' "$port"
printf '%s\n' 'Web assistants need a public HTTPS address. Put a TLS proxy or tunnel in front of Lynewright and set LYNEWRIGHT_PUBLIC_URL to that HTTPS URL.'
printf 'Admin email: %s\n' "$(read_env_value LYNEWRIGHT_BOOTSTRAP_EMAIL admin@example.com "$install_dir/.env")"
if [ -n "$generated_password" ]; then
    printf 'Generated admin password: %s\n' "$generated_password"
    printf 'The password is also saved in %s/.env. This is the only time the installer displays it.\n' "$install_dir"
else
    printf 'The existing admin password was not changed; it remains in %s/.env.\n' "$install_dir"
fi
case "$compose_driver" in
    docker) stop_command='docker compose down' ;;
    podman) stop_command='podman compose down' ;;
    podman-compose) stop_command='podman-compose down' ;;
    *) stop_command='the selected Compose driver down' ;;
esac
printf 'Stop: change to %s and run %s.\n' "$install_dir" "$stop_command"
printf 'Upgrade: run sh %s/install.sh.\n' "$install_dir"
printf 'Uninstall: run sh %s/uninstall.sh; it asks before deleting data or files.\n' "$install_dir"
