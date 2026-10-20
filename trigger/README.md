# The Friday trigger

A Cloudflare Worker that starts the weekly issue at **Friday, 7 a.m. Pacific**,
on time.

GitHub's own schedule is best-effort. From September it started the Friday
10:17 UTC run between 14:27 and 17:00 UTC: four to seven hours late, and later
every week. Starting a workflow through GitHub's API takes seconds. So this
Worker keeps the clock, and once a week it asks GitHub to run
`.github/workflows/daily.yml` exactly as the schedule would.

GitHub's schedule is still there as the fallback. If the Worker fails, or its
token expires, the issue goes out late, the way it has been doing, rather than
not at all.

## What it does, and what it cannot do

- Every Friday at 7 a.m. Pacific it sends one request: run `daily.yml` on
  `main` with `automatic` set. That input makes the run behave like the schedule:
  skip if today's issue is already published, otherwise score, publish and
  email.
- Cloudflare's cron runs on UTC and ignores daylight saving. 7 a.m. Pacific
  is 14:00 UTC in summer and 15:00 in winter, so it fires at both. Each time,
  it checks the clock in Los Angeles and goes ahead only at 7. The other fire
  logs "not this hour" and does nothing; that is expected every week.
- On any day but Friday, Pacific time, it starts nothing. It only checks, by
  reading the workflow, that its token still works, and logs the answer. An
  automatic run on a Tuesday would find no Tuesday issue, build one and email
  every subscriber, so this is what makes a midweek test safe.
- It has no web address and no HTTP handler, so nobody can call it. It stores
  nothing. Its logs are kept, under the Worker's **Observability** tab.
- Its only secret is a GitHub token with one permission, **Actions: write**.
  That token can start a workflow. It cannot change code, read other secrets or
  touch another repository.

**Sending twice is guarded in two places.** Up to three automatic runs now
arrive each Friday: this one, then GitHub's 15:17 UTC and its 18:47 UTC
catch-up, both usually hours late. 15:17 UTC is 7:17 a.m. Pacific at the
earliest, so even a punctual fallback cannot go out before 7.
Only one runs at a time. The workflow's guard job skips any automatic run once
the day's page exists. Behind it, `daily-dive run` will not email a date the
`sent` table already records.

## Setup — two things, done once

### 1. Make the token on GitHub

GitHub → your avatar → **Settings** → **Developer settings** → **Personal
access tokens** → **Fine-grained tokens** → **Generate new token**.

| Field | Value |
|---|---|
| Token name | `weekly-dive-trigger` |
| Expiration | the longest it offers. Put the date in a calendar. |
| Repository access | **Only select repositories** → `Daily-Dive` |
| Repository permissions | **Actions: Read and write**. Nothing else. *Metadata: Read* is added automatically. |

Copy the token. GitHub shows it once.

### 2. Deploy the Worker on Cloudflare

Through the dashboard, the same way the other site deploys:

1. **Workers & Pages** → **Create** → **Import a repository** → pick
   `MUXWilliams/Daily-Dive`.
2. Set the **root directory** to `trigger`. Leave the build command empty;
   the deploy command `npx wrangler deploy` is the default.
3. Deploy. Cloudflare reads `wrangler.toml` from this directory for the name
   and the cron.
4. On the new Worker: **Settings** → **Variables and Secrets** → **Add** →
   type **Secret**, name `GITHUB_TOKEN`, value the token from step 1.
5. Optional: under **Settings** → **Build**, set the build watch path to
   `trigger/*`. The weekly bot commit will then stop redeploying an unchanged
   Worker.

Or from a terminal with Node installed, inside this directory:

```bash
npx wrangler login
npx wrangler deploy
npx wrangler secret put GITHUB_TOKEN     # paste the token when asked
```

A redeploy keeps the secret, so the secret is set only once.

**Check it took.** The Worker's **Settings** → **Trigger events** should show
two cron triggers, and both must say **Friday**: 14:00 and 15:00 UTC. If either
says another day, the deployed `wrangler.toml` is not this one.

## Testing it safely — any day but Friday

Cloudflare can fire the Worker's cron on demand. On a Saturday through
Thursday that press starts nothing; the Worker reads `daily.yml` with its
token and logs the result. Open the Worker's **Observability** tab
afterwards:

- `self-check ok … the token reaches daily.yml`: Friday will work.
- `self-check failed — HTTP 401` (or 403, 404): fix it from the table below,
  then test again.
- `GITHUB_TOKEN is not set`: the secret is missing (step 2.4).

**Never press it on a Friday.** On a Friday at 7 a.m. Pacific it really starts
the week's issue. At any other Friday hour it does nothing.

## How to tell it worked

On Friday, open the repository's **Actions** tab. **Build issue** should show a
run started a minute or two after 7 a.m. Pacific, triggered by `workflow_dispatch`
under your name. Hours later GitHub's own schedule arrives. Its runs end at the
guard with *"Nothing to do — another automatic run got here first."*

If no run appeared, open the Worker's **Observability** tab. Every
invocation is logged with the time the Worker thought it was in Los Angeles,
and a failure is logged with its reason. Look for one of:

- `weekly issue dispatched`: GitHub accepted it. Look at Actions again.
- `not this hour`: the other cron's fire, expected every Friday.
- `weekly-dive-trigger failed`: followed by one of the reasons below.

**No line at all for 14:00 or 15:00 UTC** means the cron never called the
Worker. That is a Cloudflare trigger problem, not this code.

The failure reasons:

| Reason | Means | Fix |
|---|---|---|
| `GITHUB_TOKEN is not set` | the secret is missing | step 2.4 |
| `HTTP 401` | the token expired or was revoked | make a new one (step 1) and replace the secret |
| `HTTP 403` | the token lacks Actions: write, or does not include Daily-Dive | edit the token's permissions |
| `HTTP 404` | wrong repository or workflow name | `REPO` / `WORKFLOW` in `worker.js` |
| `HTTP 422` | `daily.yml` no longer declares the `automatic` input | a test should have caught this first |

GitHub's late schedule covers that week either way.

## Changing it

- **The time.** It is set in three places, and `pytest` checks all three:
  - `LOCAL_HOUR` and `TIME_ZONE` in `worker.js`.
  - The two UTC crons here: the hour that is `LOCAL_HOUR` under daylight time,
    and the one that is `LOCAL_HOUR` under standard time.
  - GitHub's fallback crons in `daily.yml`, which must be no earlier than the
    later of those two UTC hours.

  Write the day as `FRI` here, never as a number. Cloudflare counts days from
  1 = Sunday, so `5` is Thursday on Cloudflare and Friday on GitHub. A test
  enforces it.
- **The code.** `node --test trigger/worker.test.mjs` runs the Worker's tests
  with a stand-in for GitHub. Nothing goes over the network. `pytest` runs them
  too, and checks that the Worker and the workflow agree on the file, the branch
  and the input.
