/**
 * Twitch dashboard tab.
 *
 * Plain IIFE — no build step. Uses window.__HERMES_PLUGIN_SDK__ globals
 * (React, components, hooks, fetchJSON) + window.__HERMES_PLUGINS__.register.
 *
 * Architecture: four INDEPENDENT cards (App, Accounts, Behavior, Test).
 * Each card owns its own state, its own save logic, and its own inline
 * result/error — there is NO global banner. They share a small set of
 * building blocks (Field, ToggleRow, Note, useSaver, postJSON) so behaviour
 * is identical everywhere: extend before recreate, reuse over re-write.
 *
 * Setup flow (all per-install, nothing hardcoded):
 *   1. App card — client id + secret from the user's own free Twitch dev app
 *      (auto-saves when a field loses focus).
 *   2. Accounts card — "Connect" per role starts the device-activation flow:
 *      the UI shows the twitch.tv/activate link (the code is in it), polls, and
 *      stores the token pair when the user authorizes on any device. The
 *      broadcaster login also sets the channel.
 *   3. Behavior card — send-as toggle, response mode, allowlist (each change
 *      auto-saves that single field immediately).
 *   4. Test card — sends a message as the active identity and shows its own
 *      result.
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

  // fetchJSON does NOT set a Content-Type header; without it the browser fetch
  // sends string bodies as text/plain and FastAPI 422s them.
  function postJSON(url, body, method) {
    return SDK.fetchJSON(url, {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  }

  var BTN_BASE = "inline-flex items-center gap-2 px-4 py-2 text-sm font-medium transition-colors cursor-pointer";
  var BTN_OUTLINE = BTN_BASE + " border border-border bg-background/40 text-foreground hover:bg-foreground/10";
  var BTN_PRIMARY = BTN_BASE + " bg-primary text-primary-foreground hover:opacity-90";
  var BTN_DANGER = BTN_BASE + " border border-destructive/40 bg-transparent text-destructive hover:bg-destructive/10";

  // ── Shared building blocks (reused by every card) ──────────────────────────

  // Field — label + input. onChange updates local state per keystroke; onBlur
  // commits (used to auto-save when the field is "done").
  function Field(props) {
    return h("div", { className: "flex flex-col gap-1" },
      h(Label, { className: "text-xs text-muted-foreground" }, props.label),
      h(Input, {
        value: props.value || "",
        type: props.type || "text",
        placeholder: props.placeholder || "",
        onChange: function (e) { if (props.onChange) props.onChange(e.target.value); },
        onBlur: props.onBlur || null
      })
    );
  }

  // ToggleRow — label + a row of option buttons.
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

  // Note — the shared inline status line. Every card renders exactly one of
  // these for its own result/error. There is no global banner: a failure always
  // shows in the card that caused it. Returns nothing for an empty note.
  function Note(props) {
    var note = props.note;
    if (!note || !note.text) return null;
    return h("p", { className: "text-xs " + (note.ok ? "text-emerald-400" : "text-red-400") },
      (note.ok ? "\u2713 " : "\u2717 ") + note.text);
  }

  // useSaver — the shared save/result state every card uses. Returns
  // { saving, note, setNote, run }. run(promise, {ok, err}) stores the human
  // result in THIS card's own `note` and resolves with {ok, result} so callers
  // can react (e.g. reload). ok/err may be a string or a fn(response)->string
  // (return ""/null to show no note). This is the one place save + result live,
  // so every card behaves identically and independently.
  function useSaver() {
    var saving = useState(false);
    var note = useState(null);
    function setNote(n) { note[1](n); }
    function run(promise, texts) {
      saving[1](true);
      note[1](null);
      return promise
        .then(function (r) {
          var t = typeof texts.ok === "function" ? texts.ok(r) : texts.ok;
          note[1](t ? { ok: true, text: t } : null);
          return { ok: true, result: r };
        })
        .catch(function (e) {
          var t = typeof texts.err === "function" ? texts.err(e) : texts.err;
          note[1](t ? { ok: false, text: t } : null);
          return { ok: false, error: e };
        })
        .finally(function () { saving[1](false); });
    }
    return { saving: saving[0], note: note[0], setNote: setNote, run: run };
  }

  // ── Card 1: App credentials (auto-save on blur — no save button) ──────────
  function AppCard(props) {
    // props: app = /status.app {client_id, has_secret}, onSaved()
    var form = useState({ client_id: "", client_secret: "" });
    var seeded = useRef(false);
    var saver = useSaver();

    // Seed the form once from the stored identity. Only on the first load, so
    // the 10s status refresh never clobbers what the user is typing.
    useEffect(function () {
      if (props.app && props.app.client_id && !seeded.current) {
        seeded.current = true;
        form[1]({ client_id: props.app.client_id, client_secret: "" });
      }
    }, [props.app]);

    function commit() {
      // Auto-save the whole form (a blank secret keeps the stored one).
      saver.run(
        postJSON(BASE + "/app", { client_id: form[0].client_id, client_secret: form[0].client_secret }),
        { ok: "App credentials saved.", err: function (e) { return "Could not save: " + e; } }
      ).then(function (out) {
        if (out.ok) {
          form[1]({ client_id: form[0].client_id, client_secret: "" }); // clear the pasted secret
          props.onSaved && props.onSaved();
        }
      });
    }

    var app = props.app || {};
    return h(Card, null,
      h(CardHeader, null,
        h("div", { className: "flex items-center gap-3" },
          h(CardTitle, { className: "text-base" }, "1 \u2022 Your Twitch app"),
          h(Badge, { variant: app.has_secret ? "default" : "outline" }, app.has_secret ? "credentials set" : "not set")
        )
      ),
      h(CardContent, { className: "flex flex-col gap-3" },
        h("p", { className: "text-xs text-muted-foreground" },
          "Create a free app at ",
          h("a", { href: "https://dev.twitch.tv/console/apps", target: "_blank", rel: "noreferrer", className: "text-primary" }, "dev.twitch.tv/console/apps"),
          " (any name, no approval). This plugin uses the device-activation flow \u2014 no real redirect URI is ever used. If the console insists on one at creation time, enter the placeholder ",
          h("code", { className: "border border-border p-1" }, "https://localhost"),
          " (satisfies the HTTPS check, never contacted). Keep Client Type = ",
          h("strong", null, "Confidential"),
          " \u2014 the plugin needs the Client Secret to auto-refresh tokens. Don't pre-register scopes; they're requested at login. Paste the Client ID and Client Secret here; they stay on this machine and save automatically."),
        h("div", { className: "flex flex-col gap-3" },
          h(Field, { label: "Client ID", value: form[0].client_id, placeholder: "e.g. xyz123...",
            onChange: function (v) { form[1]({ client_id: v, client_secret: form[0].client_secret }); },
            onBlur: commit }),
          h(Field, { label: "Client secret (leave blank to keep the current one)", value: form[0].client_secret, type: "password",
            placeholder: app.has_secret ? "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022" : "paste client secret",
            onChange: function (v) { form[1]({ client_id: form[0].client_id, client_secret: v }); },
            onBlur: commit })
        ),
        h(Note, { note: saver.note })
      )
    );
  }

  // ── Card 2: Accounts (device-activation flow per role) ─────────────────────
  function AccountRow(props) {
    // props: role, acc, flow (null | {role,...,status}), onConnect, onCancel, onDisconnect
    var flow = props.flow && props.flow.role === props.role ? props.flow : null;
    var connected = !!props.acc.has_token;
    return h(Card, { className: "border" },
      h(CardHeader, null,
        h("div", { className: "flex items-center gap-3" },
          h(CardTitle, { className: "text-base" },
            props.role === "broadcaster" ? "\u{1F399} Broadcaster \u2014 channel owner" : "\u{1F916} Bot \u2014 the account the agent chats as"),
          h(Badge, { variant: connected ? "default" : "outline" }, connected ? "connected" : "not connected")
        )
      ),
      h(CardContent, { className: "flex flex-col gap-3" },
        connected
          ? h("p", { className: "text-sm text-muted-foreground" },
              "Connected as @", props.acc.login || "?",
              "  \u2022  tokens auto-refresh \u2014 nothing for you to do")
          : h("p", { className: "text-xs text-muted-foreground" },
              props.role === "broadcaster"
                ? "Log in with your channel's account. Scopes: channel:bot (unlocks chat events for this channel), user:read:chat, user:write:chat (lets the agent post AS you when you toggle it)."
                : "Log in with your bot account. Scopes: user:read:chat, user:write:chat, user:bot (lets the agent post AS the bot)."),
        flow
          ? h("div", { className: "flex flex-col gap-3 border border-border p-4" },
              h("p", { className: "text-sm" },
                "Click this link on any device (a phone works), log in as the ",
                (flow.role === "broadcaster" ? "channel" : "bot"),
                " account, and press Authorize. The code is already in the link:  "),
              h("a", {
                href: flow.verification_uri,
                target: "_blank",
                rel: "noreferrer",
                className: "block w-full break-all border border-border p-3 text-base text-primary underline",
                style: { userSelect: "text", WebkitUserSelect: "text" }
              }, flow.verification_uri),
              h("div", { className: "flex items-center gap-2" },
                h(Button, { onClick: props.onCancel, className: BTN_DANGER }, "Cancel")),
              h("p", { className: "text-xs text-muted-foreground" },
                flow.status === "authorized"
                  ? "Authorized \u2713 \u2014 finishing up\u2026"
                  : "Waiting for authorization\u2026 (waiting for you to press Authorize on the other device)")
            )
          : h("div", { className: "flex items-center gap-2" },
              h(Button, { onClick: function () { props.onConnect(props.role); }, className: BTN_PRIMARY },
                "Connect " + (props.role === "broadcaster" ? "broadcaster" : "bot") + " account"),
              connected ? h(Button, { onClick: function () { props.onDisconnect(props.role); }, className: BTN_DANGER }, "Disconnect") : null
            )
      )
    );
  }

  function AccountsCard(props) {
    // props: accounts (from /status), onSaved()
    var flow = useState(null);
    var pollRef = useRef(null);
    var saver = useSaver();

    useEffect(function () {
      return function () { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };
    }, []);

    function startPoll(flowInfo) {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      var delay = Math.max(5, Math.round((flowInfo.interval || 5) * 1000));
      var lastStatus = flowInfo.status;
      pollRef.current = setInterval(function () {
        SDK.fetchJSON(BASE + "/connect/" + flowInfo.flow_id)
          .then(function (d) {
            if (d.status === "authorized") {
              if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
              flow[1](null);
              saver.setNote(d.warning
                ? { ok: false, text: d.warning }
                : { ok: true, text: "Connected as @" + (d.login || "?") + (d.channel_set ? ". Channel set to @" + (d.login || "?") + "." : ".") });
              props.onSaved && props.onSaved();
            } else if (d.status === "expired" || d.status === "denied" || d.status === "invalid") {
              if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
              flow[1](null);
              saver.setNote({ ok: false, text: "Device flow " + d.status + " \u2014 press Connect again." + (d.warning ? " " + d.warning : "") });
            } else if (d.status !== lastStatus) {
              // Only re-render when the status actually changes — re-rendering
              // every 5s used to flicker the card and kill text selection.
              lastStatus = d.status;
              flow[1]({ role: flowInfo.role, user_code: flowInfo.user_code, verification_uri: flowInfo.verification_uri, status: d.status });
            }
          })
          .catch(function (e) {
            var emsg = String(e);
            if (emsg.indexOf("unknown or expired flow") !== -1) {
              // The flow really is gone (e.g. dashboard restarted) — terminal.
              if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
              flow[1](null);
              saver.setNote({ ok: false, text: "Device flow lost \u2014 press Connect again." });
            } else {
              // Transient error (network blip, 5xx): keep the link visible and
              // keep polling. Losing the pairing window is worse than a one-line note.
              saver.setNote({ ok: false, text: "Flow poll error \u2014 retrying: " + emsg });
            }
          });
      }, delay);
    }

    function connect(role) {
      saver.run(
        postJSON(BASE + "/connect", { role: role }),
        { ok: null, err: function (e) { return "Could not start device flow: " + e; } }
      ).then(function (out) {
        if (out.ok) {
          var d = out.result;
          flow[1]({ role: d.role, flow_id: d.flow_id, user_code: d.user_code, verification_uri: d.verification_uri, interval: d.interval, status: "pending" });
          startPoll(d);
        }
      });
    }

    function cancelFlow() {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      flow[1](null);
      saver.setNote(null);
    }

    function disconnect(role) {
      saver.run(
        postJSON(BASE + "/account", { role: role }, "DELETE"),
        { ok: function () { return role + " disconnected."; }, err: function (e) { return "Disconnect error: " + e; } }
      ).then(function (out) { if (out.ok) props.onSaved && props.onSaved(); });
    }

    var accs = props.accounts || {};
    return h(Card, null,
      h(CardHeader, null,
        h("div", { className: "flex items-center gap-3" },
          h(CardTitle, { className: "text-base" }, "2 \u2022 Accounts"),
          h(Badge, { variant: "outline" }, "twitch.tv/activate")
        )
      ),
      h(CardContent, { className: "flex flex-col gap-4" },
        h("p", { className: "text-xs text-muted-foreground" },
          "Each Connect button opens a link that already contains your pairing code. Click it on any device (a phone works), log in as that account, and press Authorize. Tokens are stored on this machine and auto-refresh \u2014 no redirect URL, ever. After your FIRST connection, restart the gateway (hermes gateway restart) so the Twitch platform comes online; it then starts on every boot."),
        h(AccountRow, { role: "broadcaster", acc: accs.broadcaster || { has_token: false }, flow: flow[0], onConnect: connect, onCancel: cancelFlow, onDisconnect: disconnect }),
        h(AccountRow, { role: "bot", acc: accs.bot || { has_token: false }, flow: flow[0], onConnect: connect, onCancel: cancelFlow, onDisconnect: disconnect }),
        h(Note, { note: saver.note })
      )
    );
  }

  // ── Card 3: Behavior (each change auto-saves that single field) ────────────
  function BehaviorCard(props) {
    // props: settings (from /status), accounts, onSaved()
    var form = useState(null);
    var seeded = useRef(false);
    var lastSaved = useRef(null);
    var saver = useSaver();

    useEffect(function () {
      if (props.settings && !seeded.current) {
        seeded.current = true;
        var init = {
          send_as: props.settings.send_as,
          response_mode: props.settings.response_mode,
          allowed_users: props.settings.allowed_users || "",
          allow_all_users: !!props.settings.allow_all_users
        };
        form[1](init);
        lastSaved.current = init;
      }
    }, [props.settings]);

    var s = form[0] || { send_as: "bot", response_mode: "mention", allowed_users: "", allow_all_users: false };

    // Auto-save ONE field. The backend /settings is a partial update, so only
    // this field is posted — rapid changes never clobber each other.
    function saveField(field, value) {
      var next = Object.assign({}, s);
      next[field] = value;
      form[1](next);
      var body = {};
      body[field] = value;
      saver.run(postJSON(BASE + "/settings", body, "PUT"),
        {
          ok: function (r) {
            return (r && r.restart_required)
              ? "Saved \u2014 applies on the next gateway restart."
              : "Saved \u2014 applies live.";
          },
          err: function (e) { return "Save error: " + e; }
        }
      ).then(function (out) {
        if (out.ok) {
          lastSaved.current = Object.assign({}, lastSaved.current, (function () { var o = {}; o[field] = value; return o; })());
          props.onSaved && props.onSaved();
        }
      });
    }

    var accs = props.accounts || {};
    return h(Card, null,
      h(CardHeader, null,
        h("div", { className: "flex items-center gap-3" },
          h(CardTitle, { className: "text-base" }, "3 \u2022 Behavior & access"),
          h(Badge, { variant: "outline" }, s.response_mode === "all" ? "answers everything" : "mention-gated")
        )
      ),
      h(CardContent, { className: "flex flex-col gap-4" },
        h(ToggleRow, {
          label: "Send as (which identity the agent posts under)",
          options: [
            { value: "bot", label: "Bot account" + (accs.bot && accs.bot.login ? " \u2014 @" + accs.bot.login : ""), disabled: !(accs.bot && accs.bot.has_token) },
            { value: "broadcaster", label: "Broadcaster account" + (accs.broadcaster && accs.broadcaster.login ? " \u2014 @" + accs.broadcaster.login : ""), disabled: !(accs.broadcaster && accs.broadcaster.has_token) }
          ],
          value: s.send_as,
          onChange: function (v) { if (v !== s.send_as) saveField("send_as", v); }
        }),
        h(ToggleRow, {
          label: "Response mode (when the agent reacts to chat)",
          options: [
            { value: "mention", label: "@mention only (safe default)" },
            { value: "all", label: "Free response (agent may answer any message)" }
          ],
          value: s.response_mode,
          onChange: function (v) { if (v !== s.response_mode) saveField("response_mode", v); }
        }),
        h(Field, {
          label: "Allowed users (comma-separated Twitch user IDs or logins \u2014 empty = nobody may reach the agent)",
          value: s.allowed_users,
          placeholder: "e.g. 298494782324473857, somefriend",
          onChange: function (v) { form[1](Object.assign({}, s, { allowed_users: v })); },
          onBlur: function () {
            // Only save when the value actually changed since the last commit.
            if (lastSaved.current && s.allowed_users === lastSaved.current.allowed_users) return;
            saveField("allowed_users", s.allowed_users);
          }
        }),
        h("label", { className: "flex items-center gap-2 text-sm" },
          h("input", {
            type: "checkbox",
            checked: !!s.allow_all_users,
            onChange: function (e) { saveField("allow_all_users", e.target.checked); }
          }),
          "Allow ALL chat users (disables the allowlist \u2014 only for a channel you fully control)"
        ),
        h(Note, { note: saver.note })
      )
    );
  }

  // ── Card 4: Test (button + its own result; no prose) ───────────────────────
  function TestCard(props) {
    // props: channel (from /status)
    var testing = useState(false);
    var note = useState(null);

    function testMessage() {
      testing[1](true);
      note[1](null);
      postJSON(BASE + "/test", { text: "" })
        .then(function (d) {
          // /test reports failures in the BODY ({ok:false, error}) with HTTP 200,
          // so decide ok/err here — the promise does not reject.
          if (d && d.ok) {
            note[1]({ ok: true, text: "\u2713 sent to #" + (d.channel || "channel") + " as " + d.as + " \u2014 check your chat" });
          } else {
            note[1]({ ok: false, text: "\u2717 " + ((d && d.error) || "test failed") });
          }
        })
        .catch(function (e) { note[1]({ ok: false, text: "\u2717 " + e }); })
        .finally(function () { testing[1](false); });
    }

    var d = props.channel;
    return h(Card, null,
      h(CardHeader, null,
        h("div", { className: "flex items-center gap-3" },
          h(CardTitle, { className: "text-base" }, "4 \u2022 Test"),
          d ? h(Badge, { variant: "default" }, "channel: " + (d.login || d.user_id)) : h(Badge, { variant: "outline" }, "no channel yet")
        )
      ),
      h(CardContent, { className: "flex flex-col gap-3" },
        h("div", { className: "flex items-center gap-2" },
          h(Button, { onClick: testMessage, disabled: testing[0], className: BTN_OUTLINE }, testing[0] ? "Sending\u2026" : "Send a test message")
        ),
        h(Note, { note: note[0] })
      )
    );
  }

  // ── Page: loads /status and wires the independent cards together ───────────
  function TwitchPage() {
    var st = useState(null);
    var setStatus = st[1];
    var pollRef = null; // (accounts owns its own poll)

    function load() {
      SDK.fetchJSON(BASE + "/status")
        .then(function (d) { if (d) setStatus(d); })
        .catch(function () { /* transient — keep the last known state */ });
    }

    // The dashboard used to go stale (loaded /status once). Auto-refresh every
    // 10s — this only updates `st`; it never touches the cards' local form
    // fields or the active pairing card, so nothing you're editing is clobbered.
    function refreshStatus() {
      SDK.fetchJSON(BASE + "/status")
        .then(function (d) { if (d) setStatus(d); })
        .catch(function () { /* transient — keep the last known state */ });
    }

    useEffect(function () {
      load();
      var statusTimer = setInterval(refreshStatus, 10000);
      return function () { clearInterval(statusTimer); };
    }, []);

    var d = st[0] || {};
    var accs = d.accounts || {};

    return h("div", { className: "flex flex-col gap-6" },
      // Intro card (static).
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
            "Your agent lives in your channel's chat: it listens over an outbound EventSub WebSocket and replies through the Helix chat API. Everything below is YOUR setup \u2014 the app, the channel, the accounts. Nothing is shared or hardcoded. Each card saves itself and shows its own status.")
        )
      ),
      h(AppCard, { app: d.app || { client_id: "", has_secret: false }, onSaved: load }),
      h(AccountsCard, { accounts: accs, onSaved: load }),
      h(BehaviorCard, { settings: d.settings || null, accounts: accs, onSaved: load }),
      h(TestCard, { channel: d.channel || null })
    );
  }

  window.__HERMES_PLUGINS__.register("twitch-platform", TwitchPage);
})();
