#!/bin/sh
# Local installer behavior tests; container startup is covered by CI.
set -eu

repo_root=$(CDPATH='' cd "$(dirname "$0")/../.." && pwd -P)
test_root=$(mktemp -d "${TMPDIR:-/tmp}/lynewright-install-test.XXXXXX")
trap 'rm -rf "$test_root"' EXIT HUP INT TERM

fail() {
    printf 'installer test failed: %s\n' "$1" >&2
    exit 1
}

fake_bin=$test_root/bin
fixture=$test_root/source
home=$test_root/home
mkdir -p "$fake_bin" "$fixture/backend/app" "$home"
cat > "$fake_bin/docker" <<'SH'
#!/bin/sh
if [ "$1" = compose ] && [ "$2" = version ]; then
    exit 0
fi
if [ "$1" = compose ]; then
    printf '%s|%s\n' "$PWD" "$*" >> "${LYNEWRIGHT_FAKE_COMPOSE_LOG:?}"
    case "$2" in
        up)
            if [ ! -d "$PWD/data" ] || [ ! -d "$PWD/data/packages" ]; then
                printf 'installer did not create package bind directories before Compose startup\n' >&2
                exit 1
            fi
            exit 0
            ;;
        down) exit 0 ;;
        run)
            if [ "${LYNEWRIGHT_FAKE_FAIL_CONTAINER_CLEANUP:-0}" = 1 ]; then
                printf 'simulated API-container cleanup failure\n' >&2
                exit 1
            fi
            if [ -n "${LYNEWRIGHT_FAKE_PACKAGES_DIR:-}" ]; then
                "$LYNEWRIGHT_FAKE_REAL_RM" -rf "$LYNEWRIGHT_FAKE_PACKAGES_DIR"/* "$LYNEWRIGHT_FAKE_PACKAGES_DIR"/.[!.]* "$LYNEWRIGHT_FAKE_PACKAGES_DIR"/..?*
            fi
            exit 0
            ;;
    esac
fi
printf 'unexpected compose call: %s\n' "$*" >&2
exit 1
SH
cat > "$fake_bin/uname" <<'SH'
#!/bin/sh
printf 'Linux\n'
SH
chmod +x "$fake_bin/docker" "$fake_bin/uname"
cat > "$fake_bin/curl" <<'SH'
#!/bin/sh
exit 0
SH
chmod +x "$fake_bin/curl"
real_rm=$(command -v rm)
cat > "$fake_bin/rm" <<'SH'
#!/bin/sh
for candidate do
    if [ -n "${LYNEWRIGHT_FAKE_CHECKOUT:-}" ] && [ "$candidate" = "$LYNEWRIGHT_FAKE_CHECKOUT" ] && [ "${LYNEWRIGHT_FAKE_FAIL_FINAL_RM:-0}" = 1 ]; then
        printf 'simulated host permission denied removing checkout\n' >&2
        exit 1
    fi
    if [ -n "${LYNEWRIGHT_FAKE_CHECKOUT:-}" ] && [ "$candidate" = "$LYNEWRIGHT_FAKE_CHECKOUT" ] && [ -e "${LYNEWRIGHT_FAKE_ROOT_OWNED_MARKER:-}" ]; then
        printf 'simulated host permission denied for root-owned package files\n' >&2
        exit 1
    fi
done
exec "${LYNEWRIGHT_FAKE_REAL_RM:?}" "$@"
SH
chmod +x "$fake_bin/rm"

cp "$repo_root/install.sh" "$fixture/install.sh"
cp "$repo_root/uninstall.sh" "$fixture/uninstall.sh"
cp "$repo_root/docker-compose.yml" "$fixture/docker-compose.yml"
cp "$repo_root/.env.example" "$fixture/.env.example"
printf '# Lynewright\n' > "$fixture/README.md"
printf 'from fastapi import FastAPI\n' > "$fixture/backend/app/main.py"
git -C "$fixture" init -b main >/dev/null
git -C "$fixture" config user.name 'Installer test'
git -C "$fixture" config user.email 'installer@example.test'
git -C "$fixture" add -A
git -C "$fixture" commit -m fixture >/dev/null
main_revision=$(git -C "$fixture" rev-parse HEAD)
git -C "$fixture" checkout -b feature/installer-test >/dev/null
printf 'feature ref\n' > "$fixture/feature-ref.txt"
git -C "$fixture" add feature-ref.txt
git -C "$fixture" commit -m 'add feature ref' >/dev/null
feature_revision=$(git -C "$fixture" rev-parse HEAD)
git -C "$fixture" checkout main >/dev/null

source_url=file://$fixture
first_dir=$home/first
PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_DIR=$first_dir LYNEWRIGHT_REPO_URL=$source_url LYNEWRIGHT_REF=main sh "$fixture/install.sh" --no-start > "$test_root/first.out"
grep -qx 'LYNEWRIGHT_BIND=127.0.0.1' "$first_dir/.env" || fail 'the installer did not default the bind to loopback.'
grep -qx 'LYNEWRIGHT_PORT=8080' "$first_dir/.env" || fail 'the installer did not set the default port.'
grep -qx 'LYNEWRIGHT_DEMO=1' "$first_dir/.env" || fail 'the installer did not default the demo on.'
grep -Fqx "LYNEWRIGHT_UID=$(id -u)" "$first_dir/.env" || fail 'the installer did not save the host uid.'
grep -Fqx "LYNEWRIGHT_GID=$(id -g)" "$first_dir/.env" || fail 'the installer did not save the host gid.'
[ -d "$first_dir/data" ] || fail 'the installer did not create data/ during --no-start.'
[ -d "$first_dir/data/packages" ] || fail 'the installer did not create data/packages/ during --no-start.'
if owner=$(stat -c '%u' "$first_dir/data" 2>/dev/null); then
    [ "$owner" = "$(id -u)" ] || fail 'data/ is not owned by the invoking user.'
elif owner=$(stat -f '%u' "$first_dir/data" 2>/dev/null); then
    [ "$owner" = "$(id -u)" ] || fail 'data/ is not owned by the invoking user.'
else
    fail 'could not inspect data/ ownership.'
fi
if owner=$(stat -c '%u' "$first_dir/data/packages" 2>/dev/null); then
    [ "$owner" = "$(id -u)" ] || fail 'data/packages/ is not owned by the invoking user.'
elif owner=$(stat -f '%u' "$first_dir/data/packages" 2>/dev/null); then
    [ "$owner" = "$(id -u)" ] || fail 'data/packages/ is not owned by the invoking user.'
else
    fail 'could not inspect data/packages/ ownership.'
fi
grep -Fqx "LYNEWRIGHT_BOOTSTRAP_EMAIL='admin@example.com'" "$first_dir/.env" || fail 'the API-valid default admin email was not written.'
grep -qx 'Admin email: admin@example.com' "$test_root/first.out" || fail 'the default admin email was not reported.'
grep -qx 'MCP connection URL: http://127.0.0.1:8080/mcp' "$test_root/first.out" || fail 'the MCP URL was not reported.'
grep -F 'Web assistants need a public HTTPS address.' "$test_root/first.out" >/dev/null || fail 'the HTTPS requirement was not explained.'
password_one=$(sed -n 's/^Generated admin password: //p' "$test_root/first.out")
[ "${#password_one}" -ge 24 ] || fail 'the generated password was shorter than 24 characters.'
case "$password_one" in
    *[!0123456789abcdefABCDEF]*) fail 'the generated password was not hexadecimal random text.' ;;
esac
grep -Fqx 'LYNEWRIGHT_TELEGRAM_ENABLED=1' "$first_dir/.env" || fail 'a fresh install did not turn Telegram on.'
secret_one=$(sed -n 's/^LYNEWRIGHT_SECRET_KEY=//p' "$first_dir/.env")
[ "${#secret_one}" -eq 64 ] || fail 'the installer did not write a 64-character secret key.'
case "$secret_one" in
    *[!0123456789abcdefABCDEF]*) fail 'the secret key was not hexadecimal random text.' ;;
esac
platform=$(uname -s)
case "$platform" in
    MINGW*|MSYS*|CYGWIN*) ;;
    *)
        if command -v stat >/dev/null 2>&1; then
            permissions=$(stat -c '%a' "$first_dir/.env" 2>/dev/null || true)
            if [ -n "$permissions" ] && [ "$permissions" != 600 ]; then
                fail '.env permissions were not 600.'
            fi
        fi
        ;;
esac

second_dir=$home/second
PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_DIR=$second_dir LYNEWRIGHT_REPO_URL=$source_url LYNEWRIGHT_REF=main LYNEWRIGHT_ADMIN_EMAIL=U5-Installer!Tag@Example.Test sh "$fixture/install.sh" --no-start > "$test_root/second.out"
grep -Fqx "LYNEWRIGHT_BOOTSTRAP_EMAIL='u5-installer!tag@example.test'" "$second_dir/.env" || fail 'the override was not normalized and safely quoted.'
grep -Fqx 'Admin email: u5-installer!tag@example.test' "$test_root/second.out" || fail 'the quoted email was not read back in normalized form.'
password_two=$(sed -n 's/^Generated admin password: //p' "$test_root/second.out")
[ "${#password_two}" -ge 24 ] || fail 'the second generated password was shorter than 24 characters.'
[ "$password_one" != "$password_two" ] || fail 'two installer runs generated the same password.'
secret_two=$(sed -n 's/^LYNEWRIGHT_SECRET_KEY=//p' "$second_dir/.env")
[ "$secret_one" != "$secret_two" ] || fail 'two installs generated the same secret key.'

sed -i.bak 's/^LYNEWRIGHT_TELEGRAM_ENABLED=1$/LYNEWRIGHT_TELEGRAM_ENABLED=0/' "$first_dir/.env" && rm -f "$first_dir/.env.bak"
printf 'LYNEWRIGHT_UID=99999\nLYNEWRIGHT_GID=99998\nKEEP=this-is-user-data\n' >> "$first_dir/.env"
PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_DIR=$first_dir LYNEWRIGHT_REPO_URL=$source_url LYNEWRIGHT_REF=main sh "$fixture/install.sh" --no-start > "$test_root/existing.out"
grep -qx 'KEEP=this-is-user-data' "$first_dir/.env" || fail 'the installer changed an existing .env.'
grep -Fqx "LYNEWRIGHT_UID=$(id -u)" "$first_dir/.env" || fail 'the installer did not refresh the existing host uid.'
grep -Fqx "LYNEWRIGHT_GID=$(id -g)" "$first_dir/.env" || fail 'the installer did not refresh the existing host gid.'
if grep -Eq '^LYNEWRIGHT_UID=99999$|^LYNEWRIGHT_GID=99998$' "$first_dir/.env"; then fail 'the installer kept stale host ids.'; fi
grep -Fqx 'LYNEWRIGHT_TELEGRAM_ENABLED=0' "$first_dir/.env" || fail 'an upgrade overrode the operator turning Telegram off.'
[ "$(sed -n 's/^LYNEWRIGHT_SECRET_KEY=//p' "$first_dir/.env")" = "$secret_one" ] || fail 'an upgrade changed the existing secret key.'

# Installs from before the key existed get one on their next upgrade.
sed -i.bak '/^LYNEWRIGHT_SECRET_KEY=/d' "$second_dir/.env" && rm -f "$second_dir/.env.bak"
PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_DIR=$second_dir LYNEWRIGHT_REPO_URL=$source_url LYNEWRIGHT_REF=main sh "$fixture/install.sh" --no-start > "$test_root/legacy.out"
legacy_secret=$(sed -n 's/^LYNEWRIGHT_SECRET_KEY=//p' "$second_dir/.env")
[ "${#legacy_secret}" -eq 64 ] || fail 'an upgrade did not add a secret key to an older .env.'
grep -Fqx 'Generated a private encryption key for stored secrets.' "$test_root/legacy.out" || fail 'the upgrade did not report the new secret key.'

other_dir=$home/not-lynewright
mkdir -p "$other_dir"
printf 'KEEP\n' > "$other_dir/marker"
if PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_DIR=$other_dir LYNEWRIGHT_REPO_URL=$source_url LYNEWRIGHT_REF=main sh "$fixture/install.sh" --no-start > "$test_root/refused.out" 2>&1; then
    fail 'the installer accepted a non-Lynewright directory.'
fi
[ -f "$other_dir/marker" ] || fail 'the refusal changed the existing directory.'

bad_email_dir=$home/bad-email
if PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_DIR=$bad_email_dir LYNEWRIGHT_REPO_URL=$source_url LYNEWRIGHT_ADMIN_EMAIL=admin@localhost sh "$fixture/install.sh" --no-start > "$test_root/bad-email.out" 2>&1; then
    fail 'the installer accepted an email the API login validator rejects.'
fi
[ ! -e "$bad_email_dir/.env" ] || fail 'the invalid email was written to .env.'

ref_upgrade_dir=$home/ref-upgrade
PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_DIR=$ref_upgrade_dir LYNEWRIGHT_REPO_URL=$source_url LYNEWRIGHT_REF=$main_revision sh "$fixture/install.sh" --no-start > "$test_root/ref-main.out"
PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_DIR=$ref_upgrade_dir LYNEWRIGHT_REPO_URL=$source_url LYNEWRIGHT_REF=feature/installer-test sh "$fixture/install.sh" --no-start > "$test_root/ref-feature.out"
[ "$(git -C "$ref_upgrade_dir" rev-parse HEAD)" = "$feature_revision" ] || fail 'the non-main ref upgrade did not select the requested commit.'
[ -f "$ref_upgrade_dir/feature-ref.txt" ] || fail 'the non-main ref upgrade did not update the checkout.'

outside_dir=$home/from-outside
caller_dir=$test_root/caller
compose_log=$test_root/compose.log
mkdir -p "$caller_dir"
(
    cd "$caller_dir"
    PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_DIR=$outside_dir LYNEWRIGHT_REPO_URL=$source_url LYNEWRIGHT_REF=main LYNEWRIGHT_FAKE_COMPOSE_LOG=$compose_log sh "$fixture/install.sh" > "$test_root/outside-cwd.out"
)
grep -Fqx "$outside_dir|compose up -d --build --force-recreate" "$compose_log" || fail 'compose did not run from the installed checkout when the installer started elsewhere.'
grep -Fqx "Stop: change to $outside_dir and run docker compose down." "$test_root/outside-cwd.out" || fail 'the stop instructions did not name the actual Compose command.'

cleanup_failure_dir=$home/cleanup-failure
PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_DIR=$cleanup_failure_dir LYNEWRIGHT_REPO_URL=$source_url LYNEWRIGHT_REF=main sh "$fixture/install.sh" --no-start > "$test_root/cleanup-failure-install.out"
cleanup_failure_packages=$cleanup_failure_dir/data/packages
cleanup_failure_marker=$cleanup_failure_packages/root-owned.whl
mkdir -p "$cleanup_failure_packages"
printf 'simulated root-owned wheel\n' > "$cleanup_failure_marker"
if PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_FAKE_COMPOSE_LOG=$compose_log LYNEWRIGHT_FAKE_REAL_RM=$real_rm LYNEWRIGHT_FAKE_PACKAGES_DIR=$cleanup_failure_packages LYNEWRIGHT_FAKE_CHECKOUT=$cleanup_failure_dir LYNEWRIGHT_FAKE_ROOT_OWNED_MARKER=$cleanup_failure_marker LYNEWRIGHT_FAKE_FAIL_CONTAINER_CLEANUP=1 sh "$cleanup_failure_dir/uninstall.sh" --yes > "$test_root/cleanup-failure.out" 2>&1; then
    fail 'uninstall succeeded after simulated container cleanup failure.'
fi
grep -F "sudo rm -rf '$cleanup_failure_dir/data/packages'" "$test_root/cleanup-failure.out" >/dev/null || fail 'container cleanup failure did not give the manual sudo command.'
[ -e "$cleanup_failure_marker" ] || fail 'failed container cleanup unexpectedly changed the simulated root-owned file.'
[ -d "$cleanup_failure_dir" ] || fail 'failed container cleanup deleted the checkout.'

packages_dir=$first_dir/data/packages
root_owned_marker=$packages_dir/root-owned.whl
mkdir -p "$packages_dir"
printf 'simulated root-owned wheel\n' > "$root_owned_marker"
PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_FAKE_COMPOSE_LOG=$compose_log LYNEWRIGHT_FAKE_REAL_RM=$real_rm LYNEWRIGHT_FAKE_PACKAGES_DIR=$packages_dir LYNEWRIGHT_FAKE_CHECKOUT=$first_dir LYNEWRIGHT_FAKE_ROOT_OWNED_MARKER=$root_owned_marker sh "$first_dir/uninstall.sh" --yes > "$test_root/uninstall.out" 2>&1
[ ! -e "$root_owned_marker" ] || fail 'container cleanup did not clear the simulated root-owned package file.'
[ ! -e "$first_dir" ] || fail 'uninstall --yes did not remove the Lynewright checkout.'
grep -F "$first_dir|compose run --rm --no-deps --entrypoint sh api -c rm -rf /data/packages/* /data/packages/.[!.]* /data/packages/..?*" "$compose_log" >/dev/null || fail 'uninstall did not clear packages through the API container.'
grep -Fqx "$first_dir|compose down --volumes --remove-orphans --rmi local" "$compose_log" || fail 'uninstall did not request removal of locally built images.'

final_rm_dir=$home/final-rm-failure
PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_DIR=$final_rm_dir LYNEWRIGHT_REPO_URL=$source_url LYNEWRIGHT_REF=main sh "$fixture/install.sh" --no-start > "$test_root/final-rm-install.out"
if PATH=$fake_bin:$PATH HOME=$home LYNEWRIGHT_FAKE_COMPOSE_LOG=$compose_log LYNEWRIGHT_FAKE_REAL_RM=$real_rm LYNEWRIGHT_FAKE_CHECKOUT=$final_rm_dir LYNEWRIGHT_FAKE_FAIL_FINAL_RM=1 sh "$final_rm_dir/uninstall.sh" --yes > "$test_root/final-rm-failure.out" 2>&1; then
    fail 'uninstall succeeded after simulated final checkout removal failure.'
fi
grep -F "sudo rm -rf '$final_rm_dir'" "$test_root/final-rm-failure.out" >/dev/null || fail 'final checkout removal failure did not give the manual sudo command.'
[ -d "$final_rm_dir" ] || fail 'simulated final removal failure unexpectedly deleted the checkout.'

printf '%s\n' 'Installer tests: 16 passed (loopback/default email, private secret key on install and upgrade, Telegram on for fresh installs only, MCP URL and HTTPS hint, host uid/gid ownership, user-owned data/package directories before Compose, random generated credentials, existing .env preservation, invalid email refusal, unrelated path refusal, non-main ref upgrade, external-cwd startup, correct stop command, failed container cleanup safety/manual command, final removal failure/manual sudo command, simulated root-owned package cleanup, uninstall volume/images/checkouts).'
