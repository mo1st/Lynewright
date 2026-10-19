# Connect an assistant

Lynewright lets an AI assistant start production lines, follow their runs, and inspect review items. A line can stop at a human review gate. The assistant can decide that gate only when the line's **Who can decide reviews?** setting allows it.

## Before you connect

Use a public HTTPS address for web assistants. The installer binds the dashboard to this machine by default. Put a TLS proxy or secure tunnel in front of Lynewright, set its public URL in the local settings, and make sure the OAuth callback returns to that same HTTPS origin. Do not expose the MCP service directly on an untrusted network.

In **Settings → Assistants**, create a project-scoped client and grant only the tools it needs. Lynewright shows its bearer token once with the `/mcp` connection URL. Copy the token into an MCP client that accepts bearer tokens; remove the client in Settings to revoke it. Web assistants normally use the browser sign-in flow instead, so you do not paste this token into them.

## Gemini on the web

1. Open Gemini at [gemini.google.com](https://gemini.google.com/) and open **Settings & help → Connected Apps**.
2. Under **Custom apps for Spark**, enter your Lynewright HTTPS `/mcp` URL and continue.
3. Sign in to Lynewright when the OAuth page opens. Review the requested access and approve only the scopes you want to grant.
4. In Gemini Spark, select the connected app with `@` and ask it to list your lines. Then ask it to start a line by name and provide the line's required inputs.
5. Follow the run with the assistant. When the run waits for review, open **Reviews** in Lynewright to inspect it. A line set to dashboard review cannot be approved through the assistant.

Custom MCP apps in Gemini are currently limited by Google's account, location, and product availability rules. See Google's [custom app connection guide](https://support.google.com/gemini/answer/17209137).

## Claude

1. In Claude, open **Settings → Connectors** and choose **Add custom connector**.
2. Enter the Lynewright HTTPS `/mcp` URL and add the connector.
3. Connect it, sign in on the Lynewright OAuth page, and review the access request.
4. Enable the Lynewright tools you need from Claude's tools menu.
5. Ask Claude to list your lines, then start one with its required inputs. Use **Reviews** in Lynewright when a run reaches a dashboard-only review gate.

Anthropic currently offers remote custom connectors on eligible paid plans. See [Anthropic's custom remote MCP guide](https://support.anthropic.com/en/articles/11175166-about-custom-integrations-using-remote-mcp).

## ChatGPT

1. In an eligible ChatGPT workspace, ask an administrator to enable developer mode for custom apps.
2. Open **Settings → Apps → Create** and enter the Lynewright HTTPS `/mcp` endpoint.
3. Choose OAuth, scan the available tools, and finish the Lynewright sign-in and consent flow.
4. Create the app, open a new chat, and select it from the tools menu.
5. Ask it to list lines, then start one with its required inputs. Approve dashboard-only review gates from **Reviews** in Lynewright.

ChatGPT's custom MCP app availability depends on the workspace plan and admin settings. See [OpenAI's current MCP app setup guide](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt).

## Other MCP clients

1. Add a remote Streamable HTTP server using the Lynewright HTTPS `/mcp` URL.
2. Choose OAuth 2.1 and open the browser sign-in page. Review the account and requested scopes before approving.
3. If the client supports only bearer-token authentication, create a client in **Settings → Assistants** and paste its one-time token into the client's secure credential field.
4. Call `lines.list`, inspect a line with `lines.get`, and start it with `runs.start`. Use `runs.get` to follow progress and `reviews.get` to inspect a waiting item.

## Review control

New and existing lines default to **Dashboard only** review. In that mode an assistant can read the item and direct you to `/reviews`, but `reviews.decide` refuses approval or rejection. An administrator can set an individual line to **Dashboard or assistant** when assistant decisions are appropriate. A rejection requires a reason. The run timeline records the MCP client and the Lynewright account that provisioned it.

Assistant clients can read lines, runs, and waiting reviews; start, cancel, or retry runs; and decide permitted reviews. They cannot manage users, integrations, or credentials.
