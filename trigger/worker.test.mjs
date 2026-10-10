// node --test trigger/worker.test.mjs   (run by tests/test_pipeline.py when
// node is present — a file path, because Node 22 reads a directory argument as
// a file to load, not a folder to search)
//
// No network: fetch is replaced with a stub that records each request and
// answers with whatever status the test hands it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { dispatch, fire, INPUTS, REF, REPO, WORKFLOW } from "./worker.js";

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

test("only a Friday starts anything", async () => {
  // 2026-10-09 was a Friday; 10:17 UTC is the cron's own time.
  const friday = Date.UTC(2026, 9, 9, 10, 17);
  const { calls, impl } = stub(204);
  assert.deepEqual(await fire(friday, "tok", impl), { ok: true, attempt: 1 });
  assert.equal(calls.length, 1);

  for (let d = 1; d <= 6; d++) {
    const other = friday + d * 86_400_000;
    const { calls, impl } = stub(204);
    await assert.rejects(fire(other, "tok", impl), /is not a Friday/);
    assert.equal(calls.length, 0, `nothing sent on ${new Date(other).toISOString()}`);
  }
});

test("the day is judged in UTC, as the cron is", async () => {
  // 23:30 UTC Friday is already Saturday in much of the world, and still
  // Friday on the cron's clock; 00:30 UTC Saturday is still Friday in the US.
  const { impl } = stub(204);
  await fire(Date.UTC(2026, 9, 9, 23, 30), "tok", impl);
  await assert.rejects(fire(Date.UTC(2026, 9, 10, 0, 30), "tok", impl), /is not a Friday/);
});
