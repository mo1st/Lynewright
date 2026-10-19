# Lynewright CLI wrapper

Copied from Lynewright commit `2daf3a92de058cddd265bd3bc174147b002cbd97`
(`templates/cli-wrapper`) in this repository's `templates/starters/` folder.

This template exposes one trusted command as a small asynchronous API. It uses
an argv array, never a shell. The example installs ffmpeg; replace that apt
package and command.yaml for another CLI.

## Wrap a tool in four steps

1. Put the CLI install command in Containerfile and run the container as the
   existing non-root user.
2. Declare its fixed argv array, parameter schemas, input extensions/byte caps,
   output glob, timeout, output byte cap, and concurrency in command.yaml.
3. Build with read_only true, a bounded /work tmpfs, dropped capabilities, a
   PID/memory/CPU limit, and an internal-only network. Do not publish a port.
4. Import lynewright-integration.yaml, create an internal endpoint connection,
   approve only the needed OpenAPI operations, then add the async operation to
   a line. lynewright-upload sends a managed input file; the declared artifact
   output hint stores returned files as scoped Lynewright media.

The example manifest uses `https://example.com` as a valid import placeholder.
The runtime endpoint comes from the connection's `cli_thumbnail` internal
endpoint reference, which Compose points at `http://cli-thumbnail:8001`.
Compose stores managed input and output media under `/data/media` on the
persistent `lynewright-data` volume.

The wrapper fetches URL inputs only over HTTP or HTTPS, does not follow
redirects, pins the connection to the resolved address, and rejects private,
loopback, and link-local addresses by default. Keep private URL fetching off;
Lynewright sends managed inputs as bounded multipart uploads, so the wrapper does
not need to fetch from internal services.

Each job gets a fresh generated workdir. The wrapper keeps bounded progress
lines and job state in memory; restarting it loses those job records, so the
Lynewright line runner's existing retry applies. Output files remain until fetched
or the 15-minute retention TTL expires. Timeout, cancellation, and shutdown
kill the command process group on Linux.

For local checks, install `requirements-test.txt` and run `python -m pytest tests`.
The timeout process-tree and symlink tests are skipped on Windows because
Windows does not provide the same POSIX process-group and symlink semantics.
