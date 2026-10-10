# The Friday trigger

A Cloudflare Worker that starts the weekly issue at **Friday 10:17 UTC**, on
time.

GitHub's own schedule is best-effort, and since September it has started the
Friday run between 14:27 and 17:00 UTC: four to seven hours late, and later
every week. Starting a workflow through GitHub's API takes seconds. So this
Worker keeps the clock, and once a week it asks GitHub to run
`.github/workflows/daily.yml` exactly as the schedule would.

GitHub's schedule is still there as the fallback. If the Worker fails, or its
token expires, the issue goes out late, the way it has been doing, rather than
not at all.

## What it does, and what it cannot do

- Every Friday at 10:17 UTC it sends one request: run `daily.yml` on `main`
  with `automatic` set. That input makes the run behave like the schedule:
  skip if today's issue is already published, otherwise score, publish and
  email.
- It refuses to start anything on any day but Friday. Cloudflare can fire a
  cron on demand. An automatic run on a Tuesday would find no Tuesday issue,
  build one and email every subscriber.
- It has no web address and no HTTP handler, so nobody can call it. It stores
  nothing and reads nothing.
- Its only secret is a GitHub token with one permission, **Actions: write**.
  That token can start a workflow. It cannot change code, read other secrets or
  touch another repository.

**Sending twice is guarded in two places.** Up to three automatic runs now
arrive each Friday: this one, then GitHub's late 10:17 and its 14:47 catch-up.
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
`17 10 * * 5`. Do not use a "trigger now" button to test it. On any day but
Friday the Worker refuses, by design. On a Friday it would really start the
week's issue.

## How to tell it worked

On Friday, open the repository's **Actions** tab. **Build issue** should show a
run started a minute or two after 10:17 UTC, triggered by `workflow_dispatch`
under your name. Hours later GitHub's own schedule arrives. Its runs end at the
guard with *"Nothing to do — another automatic run got here first."*

If the Worker failed, its cron invocation shows as failed in Cloudflare under
the Worker's **Logs**, with the reason. The cases:

| Reason | Means | Fix |
|---|---|---|
| `GITHUB_TOKEN is not set` | the secret is missing | step 2.4 |
| `HTTP 401` | the token expired or was revoked | make a new one (step 1) and replace the secret |
| `HTTP 403` | the token lacks Actions: write, or does not include Daily-Dive | edit the token's permissions |
| `HTTP 404` | wrong repository or workflow name | `REPO` / `WORKFLOW` in `worker.js` |
| `HTTP 422` | `daily.yml` no longer declares the `automatic` input | a test should have caught this first |

GitHub's late schedule covers that week either way.

## Changing it

- **The time.** Change the cron here *and* the first cron in `daily.yml`.
  `pytest` fails until the two match. Cron is UTC and ignores daylight saving:
  10:17 UTC is 6:17 am US Eastern in summer, 5:17 am in winter.
- **The code.** `node --test trigger/worker.test.mjs` runs the Worker's tests
  with a stand-in for GitHub. Nothing goes over the network. `pytest` runs them
  too, and checks that the Worker and the workflow agree on the file, the branch
  and the input.
