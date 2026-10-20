// node --test trigger/worker.test.mjs   (run by tests/test_pipeline.py when
// node is present — a file path, because Node 22 reads a directory argument as
// a file to load, not a folder to search)
//
// No network: fetch is replaced with a stub that records each request and
// answers with whatever status the test hands it.
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { check, dispatch, fire, INPUTS, REF, REPO, WORKFLOW } from "./worker.js";
import { readFileSync } from "node:fs";

function stub(...statuses) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    const status = statuses[Math.min(calls.length - 1, statuses.length - 1)];
    return { status, text: async () => `status ${status}` };
  };
  return { calls, impl };
}

test("a 204 is success, after one request", async () => {
  const { calls, impl } = stub(204);
  assert.deepEqual(await dispatch("tok", impl), { ok: true, attempt: 1 });
  assert.equal(calls.length, 1);
});

test("it starts the right workflow, on main, as the Friday run", async () => {
  const { calls, impl } = stub(204);
  await dispatch("tok", impl);
  const { url, init } = calls[0];
  assert.equal(url, `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`);
  assert.equal(init.method, "POST");
  assert.deepEqual(JSON.parse(init.body), { ref: REF, inputs: INPUTS });
  assert.deepEqual(INPUTS, { automatic: "true" });
  assert.equal(init.headers.Authorization, "Bearer tok");
  // GitHub refuses API calls without one.
  assert.ok(init.headers["User-Agent"]);
});

test("a transient failure is retried once, then succeeds", async () => {
  const { calls, impl } = stub(502, 204);
  assert.deepEqual(await dispatch("tok", impl), { ok: true, attempt: 2 });
  assert.equal(calls.length, 2);
});

test("a bad or expired token fails at once, with the reason", async () => {
  for (const status of [401, 403, 404, 422]) {
    const { calls, impl } = stub(status);
    await assert.rejects(dispatch("tok", impl), new RegExp(`HTTP ${status}`));
    assert.equal(calls.length, 1, `no retry on ${status} — it cannot help`);
  }
});

test("repeated failure gives up loudly rather than looping", async () => {
  const { calls, impl } = stub(500, 500, 500);
  await assert.rejects(dispatch("tok", impl), /could not start the weekly issue/);
  assert.equal(calls.length, 2);
});

test("a missing token is an error, never a silent no-op", async () => {
  const { calls, impl } = stub(204);
  await assert.rejects(dispatch(undefined, impl), /GITHUB_TOKEN is not set/);
  assert.equal(calls.length, 0);
});

// The two cron times, read from wrangler.toml so the test follows the config.
const CRON_HOURS = [...readFileSync(new URL("./wrangler.toml", import.meta.url), "utf8")
  .match(/^crons = \[(.*)\]$/m)[1].matchAll(/"0 (\d+) \* \* FRI"/g)].map((m) => Number(m[1]));

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

test("summer: 14:00 UTC is 7 a.m. PDT and goes; 15:00 is 8 a.m. and does not", async () => {
  const friday = Date.UTC(2026, 9, 9); // 2026-10-09, a Friday, on daylight time
  const go = stub(204);
  assert.deepEqual(await fire(friday + 14 * HOUR, "tok", go.impl), { ok: true, attempt: 1, at: "Fri 07:00 America/Los_Angeles" });
  assert.equal(go.calls.length, 1);

  const wait = stub(204);
  const result = await fire(friday + 15 * HOUR, "tok", wait.impl);
  assert.match(result.skipped, /8:00/);
  assert.equal(wait.calls.length, 0);
});

test("winter: 15:00 UTC is 7 a.m. PST and goes; 14:00 is 6 a.m. and does not", async () => {
  const friday = Date.UTC(2026, 11, 4); // 2026-12-04, a Friday, on standard time
  const go = stub(204);
  assert.deepEqual(await fire(friday + 15 * HOUR, "tok", go.impl), { ok: true, attempt: 1, at: "Fri 07:00 America/Los_Angeles" });

  const wait = stub(204);
  const result = await fire(friday + 14 * HOUR, "tok", wait.impl);
  assert.match(result.skipped, /6:00/);
  assert.equal(wait.calls.length, 0);
});

test("every Friday for a year, through both clock changes, exactly one cron goes at 7 a.m.", async () => {
  assert.deepEqual(CRON_HOURS, [14, 15]);
  const first = Date.UTC(2026, 9, 9);
  for (let week = 0; week < 53; week++) {
    const friday = first + week * 7 * DAY;
    let sent = 0;
    for (const h of CRON_HOURS) {
      const { calls, impl } = stub(204);
      await fire(friday + h * HOUR, "tok", impl);
      sent += calls.length;
    }
    assert.equal(sent, 1, `week of ${new Date(friday).toISOString().slice(0, 10)}`);
  }
});

const WORKFLOW_URL = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}`;

test("any day but Friday, in Pacific time, only checks the token — read-only, never a dispatch", async () => {
  const friday = Date.UTC(2026, 9, 9);
  for (let d = 1; d <= 6; d++) {
    for (const h of CRON_HOURS) {
      const when = friday + d * DAY + h * HOUR;
      const { calls, impl } = stub(200);
      const result = await fire(when, "tok", impl);
      assert.match(result.checked, /nothing was started/);
      assert.equal(calls.length, 1, new Date(when).toISOString());
      assert.equal(calls[0].url, WORKFLOW_URL);
      assert.equal(calls[0].init.method, "GET");
      assert.equal(calls[0].init.body, undefined);
      assert.equal(calls[0].init.headers.Authorization, "Bearer tok");
    }
  }
  // 03:00 UTC Saturday is still Friday evening in California: judged locally.
  const { calls, impl } = stub(200);
  const late = await fire(Date.UTC(2026, 9, 10, 3), "tok", impl);
  assert.ok(late.skipped);
  assert.equal(calls.length, 0);
});

test("the midweek check fails loudly on a bad token or a wrong workflow", async () => {
  const tuesday = Date.UTC(2026, 9, 20, 14);
  for (const status of [401, 403, 404]) {
    const { calls, impl } = stub(status);
    await assert.rejects(fire(tuesday, "tok", impl), new RegExp(`self-check failed — HTTP ${status}`));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].init.method, "GET");
  }
  const { calls, impl } = stub(200);
  await assert.rejects(check(undefined, impl), /GITHUB_TOKEN is not set/);
  assert.equal(calls.length, 0);
});

test("the scheduled handler logs every outcome, and logs a failure before rethrowing it", async () => {
  const realFetch = globalThis.fetch;
  const lines = [];
  const realLog = console.log, realError = console.error;
  console.log = (m) => lines.push(["log", m]);
  console.error = (m) => lines.push(["error", m]);
  try {
    globalThis.fetch = async () => ({ status: 200, text: async () => "" });
    await worker.scheduled({ scheduledTime: Date.UTC(2026, 9, 20, 14) }, { GITHUB_TOKEN: "tok" }, {});
    assert.match(lines.at(-1)[1], /^self-check ok \(2026-10-20T14:00:00.000Z\) — Tue 07:00 America\/Los_Angeles/);

    globalThis.fetch = async () => ({ status: 204, text: async () => "" });
    await worker.scheduled({ scheduledTime: Date.UTC(2026, 9, 23, 14) }, { GITHUB_TOKEN: "tok" }, {});
    assert.match(lines.at(-1)[1], /^weekly issue dispatched \(2026-10-23T14:00:00.000Z, Fri 07:00 America\/Los_Angeles, attempt 1\)/);

    await assert.rejects(
      worker.scheduled({ scheduledTime: Date.UTC(2026, 9, 23, 14) }, {}, {}), /GITHUB_TOKEN is not set/);
    assert.deepEqual(lines.at(-1)[0], "error");
    assert.match(lines.at(-1)[1], /^weekly-dive-trigger failed .*GITHUB_TOKEN is not set/);
  } finally {
    globalThis.fetch = realFetch;
    console.log = realLog;
    console.error = realError;
  }
});
