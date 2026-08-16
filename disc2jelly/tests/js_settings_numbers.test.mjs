// DOM-less logic test for the Advanced number fields in app.js.
//
// Regression: a blank HEVC/H.264/min-length field made parseInt return NaN,
// JSON.stringify wrote it as null, and every save failed with
// "hevc_quality must be a number" — naming fields the user never touched.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const code = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");

/** Sandbox app.js over a fake DOM of {id: value} inputs. */
function load(values) {
  const nodes = {};
  for (const [id, value] of Object.entries(values)) nodes[id] = { value };
  const sandbox = {
    document: {
      addEventListener() {},
      getElementById: (id) => nodes[id] || null,
    },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  return { sandbox, nodes };
}

// --- putNumber: what the save payload gets -------------------------------

const { sandbox } = load({
  "cfg-hevc-quality": "25",
  "cfg-h264-quality": "",
  "cfg-min-title": "  700  ",
  "cfg-bad": "banana",
});
const { putNumber, setNumber } = sandbox;
assert.equal(typeof putNumber, "function", "putNumber must be defined");

const body = {};
putNumber(body, "hevc_quality", "cfg-hevc-quality");
putNumber(body, "h264_quality", "cfg-h264-quality");
putNumber(body, "min_title_seconds", "cfg-min-title");
putNumber(body, "junk", "cfg-bad");

assert.equal(body.hevc_quality, 25, "a filled field is sent as a number");
assert.equal(body.min_title_seconds, 700, "surrounding whitespace is trimmed");
assert.ok(!("h264_quality" in body), "a blank field is omitted, not sent as null");
assert.ok(!("junk" in body), "non-numeric text is omitted, not sent as NaN");
// The whole point: nothing in the payload survives a JSON round-trip as null.
assert.ok(!JSON.stringify(body).includes("null"), "payload must contain no nulls");

// --- setNumber: what openSettings writes into the form -------------------

const { sandbox: s2, nodes } = load({ "cfg-hevc-quality": "22" });
s2.setNumber("cfg-hevc-quality", undefined);
assert.equal(nodes["cfg-hevc-quality"].value, "22",
  "a missing config value must not blank the field");
s2.setNumber("cfg-hevc-quality", 30);
assert.equal(nodes["cfg-hevc-quality"].value, 30, "a real number is written");
s2.setNumber("cfg-hevc-quality", "18");
assert.equal(nodes["cfg-hevc-quality"].value, 18, "a numeric string is coerced");

assert.equal(typeof setNumber, "function");
console.log("ok - settings number fields");
