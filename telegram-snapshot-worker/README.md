# Telegram snapshot webhook dispatcher (prepared, inactive)

This Cloudflare Worker accepts one command: an authorized private Telegram
`/snapshot` message. It coordinates requests in a SQLite-backed Durable Object
and calls the fixed GitHub Actions `workflow_dispatch` endpoint. The existing
Python analyzer and Telegram delivery remain in GitHub Actions. This project
has **not** been deployed and no Telegram webhook has been configured.

## Request path

1. `POST /telegram/webhook` checks the Telegram secret-token header before
   reading the JSON body. The body limit is 8 KiB.
2. The Worker accepts only a new, exact `/snapshot` text message in the
   configured private chat from the configured user. Telegram IDs must parse as
   safe integers and are compared to decimal configuration strings.
   JSON parsing can normalize a malformed fractional numeric token to an
   integer; this comparison does not validate the original number lexeme.
   Message time must be no older than 2 minutes and no more than 30 seconds
   ahead of Worker time. The Durable Object checks age before reserving and
   again immediately before the GitHub POST. A final-age skip keeps the durable
   reservation and cooldown so the same update cannot be replayed.
3. One named Durable Object reserves the update ID in persistent storage before
   attempting the GitHub POST. It retains IDs for 48 hours, limits the state to
   1,024 IDs, and applies a 10-minute cooldown from each accepted reservation.
   At capacity it refuses new requests until entries expire; it never evicts
   an unexpired ID to make room.
4. GitHub receives the fixed repository, workflow and `main` ref plus a UTC
   `requested_at_utc` generated at dispatch time. The TG-3A workflow rechecks
   request age before Python runs. A GitHub HTTP 204 means only that GitHub
   accepted the dispatch request; it does **not** prove execution or Telegram
   delivery.

No Telegram acknowledgment is sent by this Worker. Responses to authenticated
Telegram updates have fixed status values: `ignored`, `duplicate`, `cooldown`,
`capacity`, `stale_message`, `future_message`, `accepted`, `rejected`, or
`unknown`. A malformed request is `invalid_request`; bad webhook authentication
is `unauthorized`. Unknown and rejected GitHub outcomes return HTTP 200 to
prevent an automatic Telegram webhook retry after a possibly transmitted POST.
The state moves from `dispatching` to `accepted`, `rejected`, or `unknown`.
A final-age skip retains the `dispatching` reservation until its replay window
expires, but makes no GitHub POST.
A restart while dispatching leaves a durable reservation and the update is not
retried. This at-most-once choice may lose a request. A new update after the
cooldown may still produce a second snapshot for the same candle; this Worker
does not implement cross-run candle deduplication.

## Configuration

`wrangler.jsonc` contains only nonsecret values. The fixed GitHub owner,
repository, workflow, ref and 600-second cooldown are validated by the code;
changing them requires a reviewed code change. The `SNAPSHOT_COORDINATOR`
Durable Object binding and SQLite migration `v1` are in the same file.
`workers_dev` is explicitly enabled to preserve the existing
`telegram-snapshot-dispatcher.kost9klinov.workers.dev` endpoint on future
deployments to the correct Cloudflare account. `preview_urls` remains disabled
and `routes` is empty; no custom domain is configured here. Confirm the
account, endpoint, and any dashboard-managed routes before and after deployment.

The four required Worker secrets are declared by name in `secrets.required`:

- `TELEGRAM_WEBHOOK_SECRET` — the random token that must match Telegram's
  `setWebhook` secret-token setting. Use Telegram's allowed characters.
- `GITHUB_DISPATCH_TOKEN` — a short-lived, fine-grained credential restricted
  to this repository with Actions write permission.
- `ALLOWED_TELEGRAM_USER_ID` — the authorized Telegram user's decimal ID.
- `ALLOWED_TELEGRAM_CHAT_ID` — the authorized private chat's decimal ID.

The two IDs are also secrets: they are not in tracked Wrangler variables.
Both must be canonical positive decimal private user-dialog IDs in the range
`1` through `1099511627775`, as documented for [Telegram user dialog IDs](https://core.telegram.org/api/bots/ids).
The Worker returns an unavailable response without dispatching if any required
binding is missing, malformed, or outside that range. Wrangler validates
declared secret names during deployment; the Worker validates their values at
request time.

For a future local development environment, copy `.dev.vars.example` to
`.dev.vars` and replace **all four dummy values** with private local test
values. `.dev.vars`, `.env`, and their variants are ignored by Git. Use either
`.dev.vars` or `.env` for local Wrangler development, not both. The example's
ID placeholders are intentionally invalid, so a copied example fails closed
until they are replaced. Offline tests use separate dummy credentials and
mocked network calls. Never put real values in
`wrangler.jsonc`, Git, logs, command-line arguments, or this README.

## Controlled deployment plan (not executed)

Future deployment and webhook changes require separate reviews and approvals:

1. Verify the intended Cloudflare account identity, permissions, plan quotas,
   and access to SQLite-backed Durable Objects. Confirm the authorized account
   is the one associated with `kost9klinov.workers.dev`.
2. Confirm `telegram-snapshot-dispatcher` exists in that account. Establish
   ownership and inspect its current routes, custom domains, `workers.dev`
   setting, preview/version URL setting, all existing
   secret binding names, other bindings, and deployments. **Stop** if deploying
   would overwrite an unreviewed Worker or retain unexpected bindings.
3. Review `wrangler.jsonc` and the exact bundled artifact from the dry run.
   Confirm `workers_dev: true`, `preview_urls: false`, `routes: []`, the fixed
   GitHub target, the `SNAPSHOT_COORDINATOR` binding, and SQLite migration `v1`.
   Confirm that the intended `workers.dev` URL remains enabled and no additional
   route or custom domain is attached.
4. If secret provisioning is separately required, create a private temporary
   directory **outside the repository** and outside synced or backed-up
   folders. Set `umask 077`; use `mktemp -d` and verify the directory is
   owner-only (`0700`). Prepare the four real values in a `.env`
   or JSON secrets file there, using a trusted editor or secret manager that
   does not leave backup or swap copies. Verify the file is owner-only (`0600`).
   The two allowed IDs must be the intended decimal Telegram IDs. Do not use
   `.dev.vars.example` values. Never type secret values into shell commands or
   arguments, and never print or log them. Passing only the private file path
   to Wrangler avoids putting the values in shell history.
5. If provisioning secrets, supply all four bindings together with the
   reviewed code using the installed Wrangler's
   `deploy --secrets-file <private absolute path>` option. First recheck the
   target account and Worker ownership. Review the complete intended binding
   set, including any pre-existing names: secrets omitted from `--secrets-file`
   may remain bound after deployment. Do not use four separate
   `wrangler secret put` commands: each can immediately deploy a new version,
   potentially exposing a partially configured Worker if a route already
   exists. The `secrets.required` declaration makes missing names a deploy
   error; the request handler still fails closed on invalid values.
6. Verify in Cloudflare that all four binding **names** exist, the Durable
   Object and migration are present, and the existing `workers.dev` URL is
   enabled while preview URLs remain disabled. Confirm no additional route or
   custom domain is attached. Do not print or retrieve secret values. If a
   temporary secrets file was used, delete it after verification. Deleting a
   file does not guarantee erasure from SSD layers, snapshots, or backups;
   avoid those locations and rotate credentials if exposure is suspected. Stop before
   attaching another route or changing Telegram's webhook.

Cloudflare ownership, quotas, existing routes, and deployment state cannot be
verified by offline tests. A dry run does not create or activate a Worker.

### Separate webhook change gates

Before any future webhook change or reactivation, verify bot ownership and
inspect the existing Telegram webhook and any polling consumers. Confirm the allowed
user/chat IDs, HTTPS endpoint ownership, and matching webhook secret. Decide how
pending Telegram updates should be handled. Obtain explicit activation
approval, perform one controlled test, and correlate the dispatch response,
GitHub run, `SNAPSHOT_OUTCOME`, and resulting Telegram delivery. Do not infer
delivery from a GitHub HTTP 204 response.

### Rollback plan

Remove or restore the Telegram webhook first to stop new commands, then
disable the Worker's `workers.dev` URL only if taking the endpoint offline is
intended. Revoke the GitHub dispatch credential and disable only the on-demand
GitHub workflow if dispatch must also stop. Leave the
independent scheduled hourly workflow untouched. Durable Object reservations
and cooldown state persist after routing changes; reverting code does not
automatically roll back SQLite migrations or erase state. Review migration
compatibility before restoring an earlier Worker version.

## Offline verification

From this directory, with Node.js 24+:

```sh
npm ci
npm run check
npm test
npm run runtime:check
npm run format:check
WRANGLER_SEND_METRICS=false npm run config:check
```

`config:check` uses Wrangler's dry run to bundle and validate the Worker; it
does not deploy. `npm test` uses in-memory transactional storage and mocked
GitHub fetch calls. `runtime:check` also exercises concurrent requests and
object eviction with real local SQLite-backed Durable Object storage, using a
test-only GitHub fetch replacement. These checks do not contact Telegram,
GitHub or Binance. Python component tests run separately from
`binance-snapshot/`.

## Limitations

- Durable Object storage prevents duplicate dispatch attempts for an update
  while its ID remains in the 48-hour window. An outage between reservation and
  POST can lose the request. Ambiguous timeouts are never retried automatically.
- The final age check occurs immediately before the GitHub POST call. An
  arbitrary process pause between that check and network transmission cannot
  be eliminated; the GitHub workflow separately validates its dispatch time.
- This Worker cannot guarantee GitHub will run the job or that Telegram will
  deliver its later snapshot. Inspect the Python job's `SNAPSHOT_OUTCOME` log.
- On-demand delivery has a separate GitHub concurrency group, so it cannot
  replace a pending scheduled job. The two jobs can send concurrently or send
  snapshots for the same candle. GitHub concurrency is not durable candle
  deduplication; the Worker limits only commands reaching this Durable Object.
- The existing `workers.dev` URL is public. Telegram webhook configuration is
  managed outside this repository; offline checks cannot verify its current
  destination or the Cloudflare account's live routing state.
