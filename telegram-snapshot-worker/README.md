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

`wrangler.jsonc` contains only nonsecret values. Replace the two `REPLACE_...`
Telegram ID placeholders with the intended decimal IDs **before any later
deployment**. The fixed GitHub owner, repository, workflow, ref and 600-second
cooldown are validated by the code; changing them requires a reviewed code
change. The Durable Object binding and SQLite migration are in the same file.
`workers_dev` is disabled and no route is configured, so this template cannot
receive public webhooks as-is.

For a future local development environment, copy `.dev.vars.example` to
`.dev.vars` and fill it privately. `.dev.vars` is ignored by Git. For a future
Cloudflare deployment, provision Worker secrets named:

- `TELEGRAM_WEBHOOK_SECRET` — the same random secret configured in Telegram's
  `setWebhook` secret-token setting. Use Telegram's allowed characters.
- `GITHUB_DISPATCH_TOKEN` — a fine-grained credential restricted to this
  repository with Actions write permission, limited lifetime and appropriate
  rotation procedures.

Never put secret values in `wrangler.jsonc`, Git, logs, or command-line
arguments. Route and webhook setup, secret creation, publication of the TG
workflow and Worker deployment are separate, reviewed activation steps.

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
- A Worker deployment requires a separately reviewed HTTPS route, Telegram
  webhook configuration, real allowed IDs and Worker secrets. None is active.
