import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { LEVELS, SCENARIOS, parseArgs, quantiles, gate, ensureRenderedTitle, documentNavigationOK, cacheDocumentKey, classify429Path } from "../scripts/school-browser-load.mjs";

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

test("SPA project hash and homepage share one HTTP cache validator", () => {
  const origin="https://x.asa-lab.ru";
  assert.equal(cacheDocumentKey(origin,"/"),"/");
  assert.equal(cacheDocumentKey(origin,"/#/projects"),"/");
  assert.equal(cacheDocumentKey(origin,"/features/electronics/"),"/features/electronics/");
  assert.notEqual(cacheDocumentKey(origin,"/#/projects"),cacheDocumentKey(origin,"/features/electronics/"));
  assert.equal(documentNavigationOK(304, true),true);
  assert.equal(documentNavigationOK(304, false),false);
});

test("12 seconds per-page thinking is the default, bounded within 4..20 seconds", () => {
  const standard=parseArgs(base);
  assert.equal(standard.thinkMs, 12000);
  assert.equal(parseArgs([...base,"--think-ms","4000"]).thinkMs,4000);
  assert.equal(parseArgs([...base,"--think-ms","20000"]).thinkMs,20000);
  for(const value of ["0","1000","3999","20001","NaN","6.5"]) {
    assert.throws(() => parseArgs([...base,"--think-ms",value]),/INVALID_THINK_TIME/);
  }
});

test("separate client-quality 429 from actual application 429, without leaking URL", () => {
  assert.equal(classify429Path("https://x.asa-lab.ru/api/diagnostics/quality?token=private"),
    "diagnostic-quality");
  assert.equal(classify429Path("https://x.asa-lab.ru/api/projects?q=private"),
    "application-or-gateway");
  assert.equal(classify429Path("invalid-uri"),"application-or-gateway");
});

test("conditional HTTP304 navigation is valid only after same-page HTTP200", () => {
  assert.equal(documentNavigationOK(200, false), true);
  assert.equal(documentNavigationOK(200, true), true);
  assert.equal(documentNavigationOK(304, false), false);
  assert.equal(documentNavigationOK(304, true), true);
  for(const bad of [0, 301, 302, 401, 403, 404, 429, 500, 503]) {
    assert.equal(documentNavigationOK(bad, true), false);
  }
});

test("wait for hydrated document title before classifying navigation failure", async () => {
  let calls = 0;
  let waited = 0;
  const page = {
    async title() { return calls++ === 0 ? "" : "ASA Lab — Электроника"; },
    async waitForFunction(f, options) {
      waited++;
      assert.equal(options.timeout, 2500);
      assert.equal(options.polling, 100);
    },
  };
  await ensureRenderedTitle(page);
  assert.equal(waited, 1);
});

test("persist a deterministic failure reason for truly missing title", async () => {
  await assert.rejects(
    () => ensureRenderedTitle({
      async title() { return ""; },
      async waitForFunction() { throw new Error("browser timeout with arbitrary URL"); },
    }),
    /TITLE_NOT_READY/,
  );
});

test("percentiles use actual observed request latencies", () => {
  assert.equal(quantiles([]),null);
  assert.deepEqual(quantiles([100,100,200,300,800]),{p50:200,p95:800,p99:800});
});
