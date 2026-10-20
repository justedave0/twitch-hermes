# twitch-hermes

A **Twitch chat platform plugin** for [Hermes Agent](https://hermes-agent.nousresearch.com).
Your agent lives in any channel's chat: it hears messages and replies there — exactly
the role the Discord adapter plays, for Twitch.

**Nothing is hardcoded.** Every install brings its own Twitch app, its own broadcaster
account and its own bot account. No shared secrets, no pre-registered users.

## Why you don't need a public URL

Inbound chat uses the **EventSub WebSocket** transport: the plugin opens an
**outbound** connection to `wss://eventsub.wss.twitch.tv/ws` and Twitch pushes
`channel.chat.message` events down it. No callback URL, no callback secret,
no inbound port, no reverse proxy. (The webhook transport — which *does* require
a public HTTPS endpoint — is not used.)

Auth uses the **device-activation flow** (`twitch.tv/activate`): you get a short
code, open the link on *any* device, log in as the account, authorize. No OAuth
redirect URI to configure in the dev console.

## The two accounts

| Account | Login scopes | What it does |
|---|---|---|
| **Broadcaster** | `channel:bot`, `user:read:chat`, `user:write:chat` | Your channel's owner account. `channel:bot` grants *your app* permission to subscribe to that channel's chat events; the login also defines **which channel** the plugin listens on. |
| **Bot** | `user:read:chat`, `user:write:chat`, `user:bot` | The bot account (e.g. `DaveGPT`). Creates the EventSub subscription and sends messages **as the bot** by default. |

A **send-as toggle** in the dashboard switches which identity the agent posts under
(`bot`, default, or `broadcaster`). Both tokens are refreshed automatically
(6-hour TTL, single-flight refresh so Twitch's one-shot refresh tokens never collide).

## Install

```bash
# from a git tag/commit of this repo
hermes plugins install justedave0/twitch-hermes --ref <40-hex-sha> --yes-deps --enable
```

Dependencies: `websockets` + `httpx` — both already ship with Hermes.

## Setup (dashboard → Twitch tab)

1. **Create your free app** at <https://dev.twitch.tv/console/apps> (any name, no
   approval, **no redirect URI needed**). Copy the Client ID and Client Secret.
2. **App card**: paste Client ID + Client Secret → *Save*.
3. **Accounts card**:
   - *Connect broadcaster* → open `id.twitch.tv/activate`, enter the code, log in as
     the **channel owner** → Authorize. This also sets the channel.
   - *Connect bot* → same, logged in as your **bot account**.
4. **Behavior card**:
   - **Send as**: `bot` (default) or `broadcaster` — applies live.
   - **Response mode**: `mention` (default — only `@<name>` reaches the agent) or
     `all` (the agent may answer any message).
   - **Mention names**: logins the mention gate looks for (defaults to the active
     account's login).
   - **Allowed users**: comma-separated Twitch user IDs or logins. **Empty = nobody**
     (safe default — the agent is deaf until you allow someone). Applies on the next
     gateway restart.
   - **Allow all users**: disables the allowlist (only for channels you control).
5. **Test card**: *Send a test message* posts to the channel as the active identity.
6. **Restart the gateway** (`hermes gateway restart`) after the first connection —
   the Twitch platform then comes up on every gateway boot.

## Where things are stored

| What | Where |
|---|---|
| Client ID, channel, accounts (id/login only), send-as, response mode, mention names, token expiries | `config.yaml` → `plugins.entries.twitch-platform.settings` |
| Client secret, access/refresh tokens (both roles), allowlist, home channel | `.env` (credential lifecycle, chmod-protected, never returned by the dashboard — presence flags only) |

No account, token or channel is written to this repo. A pack/catalog export strips
secret-shaped keys; per-install credentials stay on the installing machine.

## How a message flows

```
chat message → EventSub WS (outbound socket)
             → dedupe + drop own messages (bot id, or sent-by-us id — echo-safe in
               both send-as modes)
             → response-mode gate (mention / all)
             → gateway authz (TWITCH_ALLOWED_USERS / TWITCH_ALLOW_ALL_USERS)
             → agent turn
             → POST /helix/chat/messages as the send-as identity (500-char cap)
```

Reconnection: keepalive watchdog, exponential backoff, and the Twitch
`session_reconnect` edge-swap flow are all handled; subscriptions are re-created
after any socket loss.

## Notes & limits

- Twitch chat is one conversation per channel: all chat shares one agent session
  (like a single group chat), not per-user sessions.
- Plain text only — no images/files/markdown rendering in chat.
- `channel.chat.message` costs 1 of your app's EventSub budget (10 max) — plenty.
- Twitch pauses **chatbot verification reviews**; personal/self-hosted bots using
  the device flow + WebSocket (this plugin's model) work without verification.
- Cron delivery works: `deliver="twitch"` jobs post to the channel via a
  standalone sender (no live gateway needed).

## Uninstall

`hermes plugins disable twitch-hermes` (or remove), then delete the `TWITCH_*`
variables from `.env` and the `twitch-platform` entry from
`plugins.entries` in `config.yaml` to wipe all credentials.
