# Telegram

Lynewright can send reviews waiting, failed runs and finished runs to your Telegram, and its message agent can answer you there. Setting it up takes about a minute and needs no terminal, no public address and no webhook.

## Connect

Open **Settings → Telegram** and follow the three steps:

1. **Create your bot.** In Telegram, open @BotFather, send `/newbot`, choose a name, and paste the token it gives you. Lynewright checks the token with Telegram right away.
2. **Link your Telegram account.** Press **Open in Telegram** (or scan the QR code with your phone) and tap **Start**. The panel turns green by itself. The link is single-use, works only for you, and expires after 10 minutes.
3. **Choose what you hear about.** Switch off anything you don't want, then press **Send test message**.

Each person links their own account the same way. **Unlink** stops one account; **Disconnect Telegram** (under Advanced) removes the bot and every link.

## How messages arrive

- **Polling (default).** Lynewright asks Telegram for new messages every few seconds. This works on a laptop or a server with no public address.
- **Webhook.** If `LYNEWRIGHT_PUBLIC_URL` is a public `https://` address, **Advanced → Use webhook** has Telegram push messages instead. Lynewright registers the webhook and its secret with Telegram itself; switching back to polling removes it.

## Turning Telegram off

Telegram is on by default, but nothing contacts Telegram until a bot is connected. To switch it off for the whole server, set `LYNEWRIGHT_TELEGRAM_ENABLED=0` in `.env` and run the installer again.
