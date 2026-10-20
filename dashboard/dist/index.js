/**
 * Twitch dashboard tab.
 *
 * Plain IIFE — no build step. Uses window.__HERMES_PLUGIN_SDK__ globals
 * (React, components, hooks, fetchJSON) + window.__HERMES_PLUGINS__.register.
 *
 * Setup flow (all per-install, nothing hardcoded):
 *   1. App card — client id + secret from the user's own free Twitch dev app
 *   2. Accounts card — "Connect" per role starts the device-activation flow:
 *      the UI shows a short code + twitch.tv/activate link, polls, and stores
 *      the token pair when the user authorizes on any device. The broadcaster
 *      login also sets the channel.
 *   3. Behavior card — send-as toggle (bot | broadcaster), response mode
 *      (mention | all), mention names, allowlist.
 *   4. Test card — sends a message as the active identity.
 *
 * Tokens are never returned by the backend (has_* flags only).
 * NOTE: Tailwind classes here are limited to the precompiled set (flex,
 * items-center, gap-N, grid, grid-cols-1, p-N, border, text-sm, w-full, ...)
 * — unknown classes fail silently in the dashboard build.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var React = SDK.React;
  var components = SDK.components;
  var Card = components.Card, CardHeader = components.CardHeader,
      CardTitle = components.CardTitle, CardContent = components.CardContent,
      Badge = components.Badge, Button = components.Button,
      Input = components.Input, Label = components.Label;
  var useState = SDK.hooks.useState, useEffect = SDK.hooks.useEffect, useRef = SDK.hooks.useRef;
  var h = React.createElement;

  var BASE = "/api/plugins/twitch-platform";

  var BTN_BASE = "inline-flex items-center gap-2 px-4 py-2 text-sm font-medium transition-colors cursor-pointer";
  var BTN_OUTLINE = BTN_BASE + " border border-border bg-background/40 text-foreground hover:bg-foreground/10";
  var BTN_PRIMARY = BTN_BASE + " bg-primary text-primary-foreground hover:opacity-90";
  var BTN_DANGER = BTN_BASE + " border border-destructive/40 bg-transparent text-destructive hover:bg-destructive/10";

  function Field(props) {
    return h("div", { className: "flex flex-col gap-1" },
      h(Label, { className: "text-xs text-muted-foreground" }, props.label),
      h(Input, {
        value: props.value || "",
        type: props.type || "text",
        placeholder: props.placeholder || "",
        onChange: function (e) { props.onChange(e.target.value); }
      })
    );
  }

  function ToggleRow(props) {
    // props: label, options: [{value, label, disabled?}], value, onChange
    return h("div", { className: "flex flex-col gap-1" },
      h(Label, { className: "text-xs text-muted-foreground" }, props.label),
      h("div", { className: "flex items-center gap-2" },
        props.options.map(function (o) {
          var active = o.value === props.value;
          return h(Button, {
            key: o.value,
            disabled: !!o.disabled,
            onClick: function () { props.onChange(o.value); },
            className: BTN_BASE + " " + (active ? BTN_PRIMARY : BTN_OUTLINE) + " flex-1"
          }, o.label);
        })
      )
    );
  }

  function AccountRow(props) {
    // props: role, acc, flow (null | {user_code, verification_uri, status}),
    //        onConnect, onCancel, onDisconnect
    var flow = props.flow && props.flow.role === props.role ? props.flow : null;
    var connected = !!props.acc.has_token;
    return h(Card, { className: "border" },
      h(CardHeader, null,
        h("div", { className: "flex items-center gap-3" },
          h(CardTitle, { className: "text-base" },
            props.role === "broadcaster" ? "\u{1F399} Broadcaster — channel owner" : "\u{1F916} Bot — the account the agent chats as"),
          h(Badge, { variant: connected ? "default" : "outline" },
            connected ? "connected" : "not connected")
        )
      ),
      h(CardContent, { className: "flex flex-col gap-3" },
        connected
          ? h("p", { className: "text-sm text-muted-foreground" },
              "Connected as @", props.acc.login || "?",
              props.acc.expires_in !== null && props.acc.expires_in !== undefined
                ? "  \u2022  token " + (props.acc.expires_in <= 0 ? "expired (auto-refreshes)" : "valid ~" + Math.round(props.acc.expires_in / 60) + " min")
                : "")
          : h("p", { className: "text-xs text-muted-foreground" },
              props.role === "broadcaster"
                ? "Log in with your channel's account. Scopes: channel:bot (unlocks chat events for this channel), user:read:chat, user:write:chat (lets the agent post AS you when you toggle it)."
                : "Log in with your bot account. Scopes: user:read:chat, user:write:chat, user:bot (lets the agent post AS the bot)."),
        flow
          ? h("div", { className: "flex flex-col gap-2 border border-border p-3" },
              h("p", { className: "text-sm" },
                "1. Open this link on any device:  ",
                h("a", { href: flow.verification_uri, target: "_blank", rel: "noreferrer", className: "text-primary" },
                  "id.twitch.tv/activate"),
                "   2. Enter the code:  "),
              h("div", { className: "flex items-center gap-2" },
                h("code", { className: "border border-border p-2 text-lg w-full" }, flow.user_code || "\u2026"),
                h(Button, { onClick: props.onCancel, className: BTN_DANGER }, "Cancel")
              ),
              h("p", { className: "text-xs text-muted-foreground" },
                flow.status === "authorized"
                  ? "Authorized \u2713 \u2014 finishing up\u2026"
                  : "Waiting for authorization\u2026 (log in as the " + flow.role + " account, then click Authorize)")
            )
          : h("div", { className: "flex items-center gap-2" },
              h(Button, { onClick: function () { props.onConnect(props.role); }, className: BTN_PRIMARY },
                "Connect " + (props.role === "broadcaster" ? "broadcaster" : "bot") + " account"),
              connected ? h(Button, { onClick: function () { props.onDisconnect(props.role); }, className: BTN_DANGER }, "Disconnect") : null
            )
      )
    );
  }

  function TwitchPage() {
    var st = useState(null);              // /status payload
    var setStatus = st[1];
    var savingApp = useState(false);
    var setSavingApp = savingApp[1];
    var app = useState({ client_id: "", client_secret: "" });
    var flow = useState(null);            // active device flow
    var settings = useState(null);
    var msg = useState(null);             // {ok, text}
    var testing = useState(false), setTesting = useState(false);
    var pollRef = useRef(null);

    function load() {
      SDK.fetchJSON(BASE + "/status")
        .then(function (d) {
          setStatus(d);
          if (d && d.settings) {
            settings[1]({
              send_as: d.settings.send_as,
              response_mode: d.settings.response_mode,
              mention_names: (d.settings.mention_names || []).join(", "),
              allowed_users: d.settings.allowed_users || "",
              allow_all_users: !!d.settings.allow_all_users
            });
          }
        })
        .catch(function (e) { msg[1]({ ok: false, text: "Load error: " + e }); });
    }

    useEffect(function () {
      load();
      return function () { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };
    }, []);

    function setMsg(ok, text) { msg[1](ok === null ? null : { ok: ok, text: text }); }

    function saveApp() {
      setSavingApp(true);
      setMsg(null, "");
      SDK.fetchJSON(BASE + "/app", {
        method: "POST",
        body: JSON.stringify({ client_id: app[0].client_id, client_secret: app[0].client_secret })
      })
        .then(function (d) { setMsg(true, d && d.ok ? "App credentials saved." : "Saved."); load(); })
        .catch(function (e) { setMsg(false, "Save error: " + e); })
        .finally(function () { setSavingApp(false); app[1]({ client_id: app[0].client_id, client_secret: "" }); });
    }

    function startPoll(flowInfo) {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      var delay = Math.max(5, Math.round((flowInfo.interval || 5) * 1000));
      pollRef.current = setInterval(function () {
        SDK.fetchJSON(BASE + "/connect/" + flowInfo.flow_id)
          .then(function (d) {
            if (d.status === "authorized") {
              if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
              flow[1](null);
              setMsg(true, "Connected as @" + (d.login || "?") + "." + (d.channel_set ? " Channel set to @" + (d.login || "?") + "." : ""));
              load();
            } else if (d.status === "expired" || d.status === "denied" || d.status === "invalid") {
              if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
              flow[1](null);
              setMsg(false, "Device flow " + d.status + " \u2014 press Connect again.");
            } else {
              flow[1]({ role: flowInfo.role, user_code: flowInfo.user_code, verification_uri: flowInfo.verification_uri, status: d.status });
            }
          })
          .catch(function (e) {
            if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
            flow[1](null);
            setMsg(false, "Flow error: " + e);
          });
      }, delay);
    }

    function connect(role) {
      setMsg(null, "");
      SDK.fetchJSON(BASE + "/connect", { method: "POST", body: JSON.stringify({ role: role }) })
        .then(function (d) {
          flow[1]({ role: d.role, flow_id: d.flow_id, user_code: d.user_code, verification_uri: d.verification_uri, interval: d.interval, status: "pending" });
          startPoll(d);
        })
        .catch(function (e) { setMsg(false, "Could not start device flow: " + e); });
    }

    function cancelFlow() {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      flow[1](null);
      setMsg(null, "Flow cancelled (the code simply expires if unused).");
    }

    function disconnect(role) {
      SDK.fetchJSON(BASE + "/account", { method: "DELETE", body: JSON.stringify({ role: role }) })
        .then(function () { setMsg(true, role + " disconnected."); load(); })
        .catch(function (e) { setMsg(false, "Disconnect error: " + e); });
    }

    function saveSettings() {
      setMsg(null, "");
      var s = settings[0] || {};
      SDK.fetchJSON(BASE + "/settings", {
        method: "PUT",
        body: JSON.stringify({
          send_as: s.send_as,
          response_mode: s.response_mode,
          mention_names: String(s.mention_names || "").split(","),
          allowed_users: s.allowed_users,
          allow_all_users: s.allow_all_users
        })
      })
        .then(function (d) {
          setMsg(true, d && d.restart_required
            ? "Saved \u2014 allowlist changes apply on the next gateway restart."
            : "Saved \u2014 send-as / response mode apply immediately.");
        })
        .catch(function (e) { setMsg(false, "Save error: " + e); });
    }

    function testMessage() {
      setTesting(true);
      setMsg(null, "");
      SDK.fetchJSON(BASE + "/test", { method: "POST", body: JSON.stringify({ text: "" }) })
        .then(function (d) {
          if (d && d.ok) {
            setMsg(true, "\u2713 sent to #" + (d.channel || "channel") + " as " + d.as + " \u2014 check your chat");
          } else {
            setMsg(false, "\u2717 " + ((d && d.error) || "test failed"));
          }
        })
        .catch(function (e) { setMsg(false, "\u2717 " + e); })
        .finally(function () { setTesting(false); });
    }

    var d = st[0];
    var s = settings[0] || { send_as: "bot", response_mode: "mention", mention_names: "", allowed_users: "", allow_all_users: false };
    var accs = (d && d.accounts) || {};

    return h("div", { className: "flex flex-col gap-6" },
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center gap-3" },
            h(CardTitle, { className: "text-lg" }, "Twitch"),
            h(Badge, { variant: "outline" }, "no public URL needed"),
            h(Badge, { variant: "outline" }, "device activation")
          )
        ),
        h(CardContent, { className: "flex flex-col gap-4" },
          h("p", { className: "text-sm text-muted-foreground" },
            "Your agent lives in your channel's chat: it listens over an outbound EventSub WebSocket and replies through the Helix chat API. Everything below is YOUR setup \u2014 the app, the channel, the accounts. Nothing is shared or hardcoded.")
        )
      ),

      // ── App credentials ──────────────────────────────────────────────
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center gap-3" },
            h(CardTitle, { className: "text-base" }, "1 \u2022 Your Twitch app"),
            h(Badge, { variant: (d && d.app && d.app.has_secret) ? "default" : "outline" },
              (d && d.app && d.app.has_secret) ? "credentials set" : "not set")
          )
        ),
        h(CardContent, { className: "flex flex-col gap-3" },
          h("p", { className: "text-xs text-muted-foreground" },
            "Create a free app at ",
            h("a", { href: "https://dev.twitch.tv/console/apps", target: "_blank", rel: "noreferrer", className: "text-primary" }, "dev.twitch.tv/console/apps"),
            " (any name, no approval needed). NO redirect URI is required \u2014 this plugin uses the device-activation flow. Paste the Client ID and Client Secret here; they stay on this machine."),
          h("div", { className: "flex flex-col gap-3" },
            Field({ label: "Client ID", value: (d && d.app && d.app.client_id) || app[0].client_id, placeholder: "e.g. xyz123...", onChange: function (v) { app[1]({ client_id: v, client_secret: app[0].client_secret }); } }),
            Field({ label: "Client secret (leave blank to keep the current one)", value: "", type: "password", placeholder: (d && d.app && d.app.has_secret) ? "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022" : "paste client secret", onChange: function (v) { app[1]({ client_id: app[0].client_id, client_secret: v }); } })
          ),
          h("div", { className: "flex items-center gap-2" },
            h(Button, { onClick: saveApp, disabled: savingApp[0], className: BTN_PRIMARY }, savingApp[0] ? "Saving\u2026" : "Save app credentials")
          )
        )
      ),

      // ── Accounts ─────────────────────────────────────────────────────
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center gap-3" },
            h(CardTitle, { className: "text-base" }, "2 \u2022 Accounts"),
            h(Badge, { variant: "outline" }, "twitch.tv/activate")
          )
        ),
        h(CardContent, { className: "flex flex-col gap-4" },
          h("p", { className: "text-xs text-muted-foreground" },
            "Each Connect button gives you a short code. Open the link on any device (phone works), log in as that account, and authorize. The token is stored on this machine and auto-refreshes \u2014 no redirect URL, ever."),
          h(AccountRow, { role: "broadcaster", acc: accs.broadcaster || { has_token: false }, flow: flow[0], onConnect: connect, onCancel: cancelFlow, onDisconnect: disconnect }),
          h(AccountRow, { role: "bot", acc: accs.bot || { has_token: false }, flow: flow[0], onConnect: connect, onCancel: cancelFlow, onDisconnect: disconnect })
        )
      ),

      // ── Behavior ─────────────────────────────────────────────────────
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center gap-3" },
            h(CardTitle, { className: "text-base" }, "3 \u2022 Behavior & access"),
            h(Badge, { variant: "outline" }, s.response_mode === "all" ? "answers everything" : "mention-gated")
          )
        ),
        h(CardContent, { className: "flex flex-col gap-4" },
          ToggleRow({
            label: "Send as (which identity the agent posts under)",
            options: [
              { value: "bot", label: "Bot account" + (accs.bot && accs.bot.login ? " \u2014 @" + accs.bot.login : ""), disabled: !(accs.bot && accs.bot.has_token) },
              { value: "broadcaster", label: "Broadcaster account" + (accs.broadcaster && accs.broadcaster.login ? " \u2014 @" + accs.broadcaster.login : ""), disabled: !(accs.broadcaster && accs.broadcaster.has_token) }
            ],
            value: s.send_as,
            onChange: function (v) { settings[1](Object.assign({}, s, { send_as: v })); }
          }),
          ToggleRow({
            label: "Response mode (when the agent reacts to chat)",
            options: [
              { value: "mention", label: "@mention only (safe default)" },
              { value: "all", label: "Free response (agent may answer any message)" }
            ],
            value: s.response_mode,
            onChange: function (v) { settings[1](Object.assign({}, s, { response_mode: v })); }
          }),
          Field({ label: "Mention names (comma-separated logins the @mention gate looks for \u2014 defaults to the active account's login)", value: s.mention_names, placeholder: "e.g. davegpt, justedave", onChange: function (v) { settings[1](Object.assign({}, s, { mention_names: v })); } }),
          Field({ label: "Allowed users (comma-separated Twitch user IDs or logins \u2014 empty = nobody may reach the agent)", value: s.allowed_users, placeholder: "e.g. 298494782324473857, somefriend", onChange: function (v) { settings[1](Object.assign({}, s, { allowed_users: v })); } }),
          h("label", { className: "flex items-center gap-2 text-sm" },
            h("input", {
              type: "checkbox",
              checked: !!s.allow_all_users,
              onChange: function (e) { settings[1](Object.assign({}, s, { allow_all_users: e.target.checked })); }
            }),
            "Allow ALL chat users (disables the allowlist \u2014 only for a channel you fully control)"
          ),
          h("div", { className: "flex items-center gap-2" },
            h(Button, { onClick: saveSettings, className: BTN_PRIMARY }, "Save behavior"),
            h("span", { className: "text-xs text-muted-foreground" }, "send-as & response mode apply live; the allowlist applies on the next gateway restart")
          )
        )
      ),

      // ── Test ─────────────────────────────────────────────────────────
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center gap-3" },
            h(CardTitle, { className: "text-base" }, "4 \u2022 Test"),
            d && d.channel ? h(Badge, { variant: "default" }, "channel: " + (d.channel.login || d.channel.user_id)) : h(Badge, { variant: "outline" }, "no channel yet")
          )
        ),
        h(CardContent, { className: "flex flex-col gap-3" },
          h("div", { className: "flex items-center gap-2" },
            h(Button, { onClick: testMessage, disabled: testing[0], className: BTN_OUTLINE }, testing[0] ? "Sending\u2026" : "Send a test message")
          ),
          h("p", { className: "text-xs text-muted-foreground" },
            "Sends \u201c\u{1F44B} test message from your Hermes agent\u201d to the channel using the current send-as identity. After your FIRST connection, restart the gateway (hermes gateway restart) so the Twitch platform comes online \u2014 the listener then starts automatically on every gateway boot."),
          msg[0] ? h("p", { className: "text-xs " + (msg[0].ok ? "text-emerald-400" : "text-red-400") }, msg[0].text) : null
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("twitch-platform", TwitchPage);
})();
