// Starts the weekly issue on time.
//
// GitHub's own cron is best-effort. Since September the Friday 10:17 schedule
// in .github/workflows/daily.yml has started between 14:27 and 17:00 UTC —
// four to seven hours late, and later every week. A workflow_dispatch is an
// ordinary API call, and GitHub starts the run it asks for within seconds. So
// this Worker owns the timing, and GitHub's schedule is kept only as the
// fallback if this ever fails.
//
// It does one thing: POST one dispatch. It holds no state and reads nothing,
// and its only secret is a GitHub token limited to Actions: write — enough to
// start a run, not enough to change any code. See trigger/README.md.
//
// Sending twice is not a risk this file has to manage. The workflow's guard
// job skips any automatic run once that day's issue is published, so a second
// dispatch, or GitHub's late schedule arriving after this one, finds the page
// and stops.

export const REPO = "MUXWilliams/daily-dive";
export const WORKFLOW = "daily.yml";
export const REF = "main";

// The input that makes a dispatched run behave exactly like the Friday
// schedule: skip if today's issue is already published, otherwise score,
// publish and send. Must match an input in .github/workflows/daily.yml — GitHub
// rejects a dispatch carrying an unknown input with a 422, and a test compares
// the two before that can happen.
export const INPUTS = { automatic: "true" };

// A dispatch is cheap and idempotent at the workflow end, so one retry costs
// nothing and covers a momentary GitHub hiccup. More than that is not the
// Worker's job: the late schedule is the real fallback.
const ATTEMPTS = 2;

export async function dispatch(token, fetchImpl = fetch) {
  if (!token) {
    // Loud in Cloudflare's cron log. Silence here would look like success.
    throw new Error("GITHUB_TOKEN is not set on this Worker");
  }
  let last;
  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    const resp = await fetchImpl(
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        // GitHub rejects API requests with no User-Agent.
        "User-Agent": "weekly-dive-trigger",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: REF, inputs: INPUTS }),
    });
    // 204 No Content is the only success the dispatches endpoint returns.
    if (resp.status === 204) return { ok: true, attempt };
    last = `HTTP ${resp.status}: ${(await resp.text()).slice(0, 300)}`;
    // 401 and 403 mean the token is wrong, expired or lacks Actions: write;
    // 404 a wrong repo or workflow name; 422 an input the workflow does not
    // declare. Retrying fixes none of those, so fail at once with the reason.
    if ([401, 403, 404, 422].includes(resp.status)) break;
  }
  throw new Error(`could not start the weekly issue — ${last}`);
}

// An automatic run on any other day is not harmless: the guard finds no page
// for that date, so it builds a fresh issue and emails every subscriber.
// Cloudflare's dashboard can fire a cron on demand, and a test press on a
// Tuesday must not do that. Manual issues go through GitHub's Run workflow.
export async function fire(scheduledTime, token, fetchImpl = fetch) {
  const day = new Date(scheduledTime).getUTCDay();
  if (day !== 5) {
    throw new Error(
      `refusing to start the weekly issue: ${new Date(scheduledTime).toISOString()} is not a Friday (UTC)`);
  }
  return dispatch(token, fetchImpl);
}

export default {
  // The cron in wrangler.toml calls this. Throwing marks the invocation as
  // failed in Cloudflare's dashboard, which is where a failure is visible.
  async scheduled(controller, env, ctx) {
    const result = await fire(controller.scheduledTime, env.GITHUB_TOKEN);
    console.log(`weekly issue dispatched (attempt ${result.attempt})`);
  },
};
