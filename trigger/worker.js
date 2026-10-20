// Starts the weekly issue on time: Friday, 7 a.m. Pacific.
//
// GitHub's own cron is best-effort. Since September its Friday 10:17 UTC
// schedule had started between 14:27 and 17:00 UTC — four to seven hours late,
// and later every week. A workflow_dispatch is an
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

const HEADERS = (token) => ({
  Authorization: `Bearer ${token}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  // GitHub rejects API requests with no User-Agent.
  "User-Agent": "weekly-dive-trigger",
});

function requireToken(token) {
  if (!token) {
    // Loud in Cloudflare's cron log. Silence here would look like success.
    throw new Error("GITHUB_TOKEN is not set on this Worker");
  }
}

export async function dispatch(token, fetchImpl = fetch) {
  requireToken(token);
  let last;
  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    const resp = await fetchImpl(
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
      method: "POST",
      headers: { ...HEADERS(token), "Content-Type": "application/json" },
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

// The safe test. A read of the workflow, with the same token, proves what a
// Friday dispatch depends on — the secret is set, the token is valid and
// reaches Daily-Dive, and daily.yml resolves — and starts nothing. Actions:
// Read and write covers the read; no extra permission.
export async function check(token, fetchImpl = fetch) {
  requireToken(token);
  const resp = await fetchImpl(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}`,
    { method: "GET", headers: HEADERS(token) });
  if (resp.status === 200) return { ok: true };
  throw new Error(
    `self-check failed — HTTP ${resp.status}: ${(await resp.text()).slice(0, 300)}`);
}

// When the issue goes out, in the editor's own time. Cloudflare's cron is UTC
// only and knows nothing of daylight saving, and 7 a.m. Pacific is 14:00 UTC in
// summer but 15:00 in winter. So wrangler.toml fires at both, and this decides
// which of the two is the real one, by asking what time it is in Los Angeles.
// The other returns quietly; it is expected every week, not a failure.
export const TIME_ZONE = "America/Los_Angeles";
export const LOCAL_HOUR = 7;

export function localTime(when) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: TIME_ZONE, weekday: "short", hour: "numeric", minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(when));
  const get = (type) => parts.find((p) => p.type === type).value;
  return {
    weekday: get("weekday"),
    hour: Number(get("hour")),
    label: `${get("weekday")} ${get("hour").padStart(2, "0")}:${get("minute")} ${TIME_ZONE}`,
  };
}

export async function fire(scheduledTime, token, fetchImpl = fetch) {
  const { weekday, hour, label } = localTime(scheduledTime);
  // An automatic run on any other day is not harmless: the guard finds no page
  // for that date, so it builds a fresh issue and emails every subscriber.
  // So any day but Friday only checks the token, read-only, and starts
  // nothing. That makes Cloudflare's test trigger safe to press midweek, and
  // its log says whether Friday would work. Manual issues go through GitHub's
  // Run workflow.
  if (weekday !== "Fri") {
    await check(token, fetchImpl);
    return { ok: true, checked: `${label}: not Friday, so nothing was started; the token reaches ${WORKFLOW}` };
  }
  if (hour !== LOCAL_HOUR) {
    return { ok: true, skipped: `${label}: not ${LOCAL_HOUR}:00, the other cron covers that` };
  }
  return { ...(await dispatch(token, fetchImpl)), at: label };
}

export default {
  // The cron in wrangler.toml calls this. Every outcome is logged with the
  // Pacific time the Worker computed, and a failure is logged before it is
  // rethrown, so the Observability tab shows what happened and why. Throwing
  // marks the invocation as failed in Cloudflare's dashboard.
  async scheduled(controller, env, ctx) {
    const when = new Date(controller.scheduledTime).toISOString();
    try {
      const result = await fire(controller.scheduledTime, env.GITHUB_TOKEN);
      if (result.checked) console.log(`self-check ok (${when}) — ${result.checked}`);
      else if (result.skipped) console.log(`not this hour (${when}) — ${result.skipped}`);
      else console.log(`weekly issue dispatched (${when}, ${result.at}, attempt ${result.attempt})`);
    } catch (err) {
      console.error(`weekly-dive-trigger failed (${when}): ${err.message}`);
      throw err;
    }
  },
};
