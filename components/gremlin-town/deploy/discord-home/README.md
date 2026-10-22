# E23: mail pings and Diablo's Discord home

This release uses the **existing `hermes-wa` profile and gateway**. It does not
create another agent, gateway, token store or mail scorer. It adds no tool text.
Telegram stays connected with its existing home and login.

The canonical settings remain `[discord]` in GT's protected configuration:

```toml
[discord]
enabled = false
mail_pings = false
diablo_home = false
```

The configured `home_channel_id` is the existing Discord conversation destination.
Do not print it, credentials, message identifiers, or mail content during install.
The two flags are separate install gates: each change must be projected with its
own checked command after changing canonical configuration. They are not dynamic
Hermes settings; editing GT's flag alone does not change a running Hermes profile.

## What changes

| Action | Existing profile file | Narrow change |
| --- | --- | --- |
| `mail-pings` | `mail-monitor/delivery.json` | Non-secret projection of the mail flag and destination; absent means off. |
| `home-channel` | `.env` | Only `DISCORD_HOME_CHANNEL`; all other lines retained. |
| `wake-route` | `webhook_subscriptions.json` | Only `gremlin-town-wake.deliver`, selecting native Discord or Telegram home. |

The wake route retains its existing secret, prompt, skills, and
`hermes-telegram` toolset. No toolset is added or enlarged. The installer refuses
an unfamiliar route rather than trying to repair it. An existing explicit
`deliver_extra` destination also stops the change; inspect it privately first.
The whole JSON document may be re-indented, but unrelated values remain intact.
The environment edit understands ordinary single-line assignments and comments.
It refuses multiline quoted values anywhere in `.env`, or unfamiliar syntax,
before preparing a change; it never treats text inside another value as a setting.

`mail_monitor.py` continues to score with the same classifier, threshold, readonly
Gmail grant, cursor and short-lived flagged-mail log. When enabled it invokes the
existing `hermes send --to discord:<configured-home> --json --file -` command.
Mail content travels over stdin, never command arguments. Captured CLI output is
never printed or logged. A zero exit plus JSON `success: true` confirms delivery.
Email text cannot issue Discord mentions. No gateway or model run is started by
this delivery command.

A confirmed Discord send produces no stdout for the cron. An unsuccessful or
uncertain send returns the original ping to the existing **Telegram** cron with
a brief notice that it may also appear in Discord. There is no direct-send retry.
Keep the mail cron set to Telegram, script-only, `--no-agent`; switching its
delivery to Discord would remove the fallback. Error alerts still use that cron.

The mail cursor advances after confirmed Discord delivery or preparation of the
unchanged Telegram stdout handoff. The latter is the prior Hermes cron delivery
contract, not proof Telegram received it. Hermes owns delivery of that stdout.
A crash after a platform accepts a message but before cursor persistence may
repeat a ping. This is at-least-once delivery, not exactly once. An invalid route
fails before reading mail or advancing its cursor, and reports through the
existing alert-once path. First-run behavior is unchanged: start from now.

Telegram is also retained as Diablo's alternate conversation. Native webhook
final replies have **no new automatic cross-platform failover** in this slice.
If their Discord route fails, stop and report; do not silently patch Hermes or
reroute an uncertain reply. A later explicit, checked `wake-route` change with
`diablo_home = false` restores the Telegram destination. Switching that flag off
does not erase the configured Discord home or disable any existing conversation.

## Checked installation, one action at a time

This is a runbook, not a script to execute end to end. The mega-build operator owns
each hub step, its before/after checks and release log. Stop at the first surprise.
Do not run these commands on the PC against Brain Home. No rollback, retry,
restart, credential enrollment or network call is performed by `install.py`.

1. Independently review the source and its guarded offline tests. Stage the exact
   reviewed `mail_monitor.py` in the existing profile's `scripts/` directory and
   this installer in the release staging directory. Back up the old script before
   replacing it, preserve its owner/mode, and compare the staged bytes afterward.
   Leave both flags off and leave the existing mail cron unchanged.
2. Read the live profile configuration privately. Check the existing Discord bot
   is already configured in **this same profile**, its intended home is usable,
   Telegram remains available, and the CLI supports the documented `send`
   options/JSON shape. Do not upgrade Hermes as part of this change. Missing setup
   or consent belongs to Nic; provide the exact missing step, not an improvised
   login. Do not add a second bot/profile for Diablo.
3. Check the mail job remains script-only, every three minutes, delivering stdout
   and errors to Telegram. Pause/idle it only through the existing checked host
   procedure if source/config replacement needs a quiet boundary. Check no second
   monitor is running. Confirm the configured home is private to Nic and the bot.
4. For mail, set only the authorized canonical flag. Prepare **one** action using
   the existing host paths (commands below). Inspect the generic result and local
   plan; it contains hashes and action names, never configuration contents or IDs.
   Applying is a separate command. The plan is created exclusively, mode `0400`;
   an existing plan is never overwritten. Use a fresh plan name for a new change.
5. Apply that exact plan once. The helper rereads and compares the canonical
   config and target, writes and verifies a private backup, rechecks both inputs,
   replaces one file, and reads it back. The new mail projection and directory
   inherit the existing profile's ownership. Existing file ownership and modes
   are retained. Check those permissions from the Hermes user before resuming.
   A failure after replacement is reported as a failure; inspect before another
   action. The helper never attempts rollback.
6. For Diablo home, first wait for Diablo to be idle and check the other chat's
   live changes. Set only the authorized canonical `diablo_home` flag, then prepare
   and apply `home-channel`. Check inherited process/container environment does
   not override `.env`'s home setting. Prepare and apply `wake-route` separately;
   its preflight must match the already installed native wake route. Preserve the
   rest of `.env`, `config.yaml`, secrets, memory, sessions, other routes and skills.
7. Native Hermes may need its existing gateway restarted to load the new home.
   The operator performs that as a separate logged step only after checking idle
   state and before/after service health. This helper does not restart anything.
   Check there is still exactly one gateway and the always-loaded tool budget did
   not grow. Keep the Discord review channel out of any newly expanded free-reply
   scope; this slice makes no allowed-user/channel, mention, or trust changes.
8. Check a synthetic mail ping reaches Discord once and produces no Telegram
   copy. Exercise the fallback with an injected offline failure first; a real
   platform disruption is not required or authorized by this runbook. Check the
   existing Telegram conversation still works and a synthetic native wake's final
   response goes to the configured Discord home. Never log response text or IDs.
   Record source, offline test, installed, and live proof separately.

Example commands use existing host paths, contain no secrets/IDs, and do not send
anything. Run **one**, check, log, then take the next step. The operator supplies
the reviewed staging location and a new private plan path. Running as the host
operator may be needed to read GT's protected config; never copy it into Hermes.

```sh
python3 install.py prepare mail-pings --hermes-home /srv/hermes-example --gt-config /etc/gremlin-town/config.toml --plan /root/e23-mail.plan.json
python3 install.py apply mail-pings --hermes-home /srv/hermes-example --gt-config /etc/gremlin-town/config.toml --plan /root/e23-mail.plan.json
```

For each home step use `home-channel` or `wake-route` and a different new plan
path. Backups are private directories under the existing profile's
`discord-home-backups/`; `before` is the exact preimage, `plan.json` records
whether that target previously existed, and `applied` appears only after the
post-write check. No source configuration contents are put in plans or stdout.
Do not delete or restore these files automatically.

## Source checks

Verified official documentation on 2 October 2026; installed Hermes compatibility
is still a live preflight, not a claim made by the offline tests:

- [Native send command and stdin/JSON contract](https://hermes-agent.nousresearch.com/docs/guides/pipe-script-output).
- [Discord home and access settings](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/discord).
- [Native webhook cross-platform delivery](https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/webhook.py).
- [Cron delivery destinations](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/cron.md).

Research queries (including the fallback counter-check):

```text
site:hermes-agent.nousresearch.com "hermes send" "--to"
site:hermes-agent.nousresearch.com cron "fallback" "deliver"
site:github.com/NousResearch/hermes-agent "deliver" "telegram" "discord" "fallback"
```

Offline tests use temporary synthetic profiles, fake Gmail/scorer responses, and
an injected native CLI runner. Run with the repository's `network_guard` plugin;
no test may reach Gmail, Discord, Telegram or a model provider.
