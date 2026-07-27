// DOM-less logic test for app.js badgeForEvent (review fix #1).
// Loads static/app.js in a vm sandbox with a stub `document`, then checks
// that SSE events advance the card status badge correctly.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const code = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
const sandbox = {
  document: { addEventListener() {}, getElementById: () => null },
  console,
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);

const { badgeForEvent } = sandbox;
assert.equal(typeof badgeForEvent, "function", "badgeForEvent must be defined");

// vm-sandbox objects have different prototypes: compare field by field.
function check(out, className, text) {
  assert.ok(out, "expected a badge update, got null");
  assert.equal(out.className, className);
  assert.equal(out.text, text);
}

// 1. A "running" event with a new stage advances the badge.
const st = {};
let out = badgeForEvent(st, { job_id: "j", stage: "ENCODE", status: "running" });
check(out, "badge running", "Copying from disc…");
assert.equal(st.badgeStage, "ENCODE");

// 2. Same stage again -> no update (progress events do not churn the badge).
out = badgeForEvent(st, { job_id: "j", stage: "ENCODE", status: "running", percent: 42 });
assert.equal(out, null);

// 3. Stage transition ENCODE -> UPLOAD advances the badge.
out = badgeForEvent(st, { job_id: "j", stage: "UPLOAD", status: "running" });
check(out, "badge running", "Saving…");

// 4. APP-stage events never touch the badge (not in BADGE_TEXT).
out = badgeForEvent(st, { job_id: "j", stage: "APP", status: "running" });
assert.equal(out, null);

// 5. Per-stage "done" events (e.g. encode finished, save not started) do not
//    regress the badge.
out = badgeForEvent(st, { job_id: "j", stage: "ENCODE", status: "done", percent: 100 });
assert.equal(out, null);

// 6. Terminal events always update the badge.
out = badgeForEvent(st, { job_id: "j", stage: "DONE", status: "done", percent: 100 });
check(out, "badge done", "Done");
out = badgeForEvent(st, { job_id: "j", stage: "ERROR", status: "error" });
check(out, "badge error", "Something went wrong");
out = badgeForEvent(st, { job_id: "j", stage: "CANCELLED", status: "cancelled" });
check(out, "badge cancelled", "Cancelled");

console.log("js_badge_logic: all assertions passed");
