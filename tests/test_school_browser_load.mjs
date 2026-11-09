import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { LEVELS, SCENARIOS, parseArgs, quantiles, gate } from "../scripts/school-browser-load.mjs";

const sha = "3e8713476cafedda3e24a39665458e833efd7c22";
const base = ["--origin", "https://x.asa-lab.ru", "--revision", sha, "--stage", "15"];
const file = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../scripts/school-browser-load.mjs");

test("only exactly requested 15, 30, 50, 75, 100 stages exist", () => {
  assert.deepEqual(LEVELS, [15, 30, 50, 75, 100]);
  assert.equal(new Set(SCENARIOS.map(s => s.id)).size, SCENARIOS.length);
  assert.ok(SCENARIOS.some(s => s.id === "electronics-info"));
  assert.ok(SCENARIOS.some(s => s.id === "three-d-info"));
  assert.ok(SCENARIOS.some(s => s.id === "blocks-info"));
  assert.ok(SCENARIOS.some(s => s.id === "project-login-shell"));
});

test("plan cannot contact school or start Chrome", () => {
  for (const users of LEVELS) {
    const run = spawnSync(process.execPath, [file, ...base.slice(0, -1), String(users)], {encoding:"utf8"});
    assert.equal(run.status, 0, run.stderr);
    const out=JSON.parse(run.stdout.trim());
    assert.equal(out.mode,"PLAN_ONLY");
    assert.equal(out.networkRequests,0);
    assert.equal(out.browserLaunches,0);
    assert.equal(out.stage,users);
  }
});

test("requires explicit apply, unique output and exact commit", () => {
  assert.equal(parseArgs(base).apply, false);
  assert.throws(() => parseArgs([...base, "--apply"]), /EXCLUSIVE_OUTPUT_REQUIRED/);
  assert.equal(parseArgs([...base, "--output", "fresh.json", "--apply"]).apply, true);
  assert.throws(() => parseArgs(["--origin", "https://x.asa-lab.ru", "--revision", "deadbeef", "--stage","15"]), /EXACT_RUNTIME_SHA_REQUIRED/);
});

test("production, unexpected host and origin with userinfo/path are blocked", () => {
  for(const origin of [
    "https://asa-lab.ru", "https://www.asa-lab.ru", "https://attacker.example",
    "http://x.asa-lab.ru", "https://x.asa-lab.ru:8443",
    "https://x.asa-lab.ru/other", "https://x.asa-lab.ru/?a=b",
    "https://user:password@x.asa-lab.ru"
  ]){
    assert.throws(() => parseArgs(["--origin", origin, "--revision", sha, "--stage","15"]));
  }
});

test("reject invalid concurrency or duration", () => {
  for(const count of ["1","14","16","29","31","74","76","101","NaN"]){
    assert.throws(() => parseArgs(["--origin", "https://x.asa-lab.ru", "--revision",sha,"--stage",count]), /INVALID_STAGE/);
  }
  for(const seconds of ["0","29","181","NaN"]){
    assert.throws(() => parseArgs([...base,"--seconds",seconds]), /INVALID_DURATION/);
  }
});

test("failed health, 429, 5xx, slow stage stop escalation", () => {
  const good = { users: 15, navigationTotal: 50, navigationFailed: 0, latencies: Array(50).fill(200),
    http429: 0, http5xx:0, healthFailures:0, abortReason:null };
  assert.equal(gate(good),"PASS");
  assert.equal(gate({...good,http429:1}),"FAIL_GATEWAY");
  assert.equal(gate({...good,http5xx:1}),"FAIL_GATEWAY");
  assert.equal(gate({...good,healthFailures:1}),"FAIL_HEALTH");
  assert.equal(gate({...good,navigationFailed:5}),"FAIL_NAVIGATION");
  assert.equal(gate({...good,latencies:Array(50).fill(10001)}),"FAIL_LATENCY");
  assert.equal(gate({...good,abortReason:"SERVER_HEALTH_OR_REVISION"}),"STOP_SERVER_HEALTH_OR_REVISION");
});

test("percentiles use actual observed request latencies", () => {
  assert.equal(quantiles([]),null);
  assert.deepEqual(quantiles([100,100,200,300,800]),{p50:200,p95:800,p99:800});
});
