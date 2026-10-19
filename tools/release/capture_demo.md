# VPS demo capture steps

Use a disposable VPS with Docker Compose and a clean Lynewright checkout. Do not show credentials, private hostnames, catalogue signing material, or user data in the recording.

1. On a disposable host, check out the reviewed release commit. Run `docker compose down -v` only if this checkout already has a disposable demo volume; this removes that checkout's Compose volumes.
2. Copy `.env.example` to `.env`; set a temporary bootstrap email and password. Keep `.env` outside the recording frame.
3. Run `docker compose up --build -d`, then `docker compose ps` and wait until `api` is healthy and `dashboard` is running.
4. Open the dashboard in a private browser window and sign in using the temporary bootstrap account.
5. Open **Lines**, select **Demo: write → review → format → notify**, enter a short neutral topic, and start the run.
6. Show the run waiting for review, open **Reviews**, approve the item, then return to the run and show its completed output and application-log notification.
7. Record a short GIF that clearly shows the review gate and the completed run. Do not expose the address bar if it contains a private host name.
8. Save the approved capture to `docs/assets/demo-line.gif`. Afterwards run `docker compose down -v` on this disposable checkout and remove the temporary `.env` and credentials.

This is a capture checklist, not a deployment guide. The demo uses the bundled sample integration and does not publish external content.
