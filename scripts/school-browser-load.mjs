#!/usr/bin/env node
/**
 * ASA Lab read-only browser capacity probe, run from D2-R2 or a dedicated QA PC.
 * Simulates independent anonymous Chrome contexts, NOT authenticated pupils.
 * Never writes projects, logs in, accesses paid AI, modifies infra or retries 429/5xx.
 * The existing separately authorized HTTP load harness tests project persistence.
 *
 * Usage:
 *  node scripts/school-browser-load.mjs --origin https://x.asa-lab.ru \
 *    --revision 3e8713476cafedda3e24a39665458e833efd7c22 \
 *    --stage 15 --seconds 45 --apply --output /private/stage15.json
 *
 * The entrypoint defaults to PLAN-ONLY. No network/browser is used without --apply.
 */
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";
import process from "node:process";
import { performance } from "node:perf_hooks";

export const LEVELS = Object.freeze([15, 30, 50, 75, 100]);
export const SCENARIOS = Object.freeze([
  { id: "home", path: "/" },
  { id: "electronics-info", path: "/features/electronics/" },
  { id: "three-d-info", path: "/features/3d-modeling/" },
  { id: "blocks-info", path: "/features/block-programming/" },
  { id: "project-login-shell", path: "/#/projects" },
]);
const ALLOWED = new Set(["x.asa-lab.ru", "test2.asa-lab.ru", "test.asa-lab.ru"]);
const SHA40 = /^[a-f0-9]{40}$/;

export function parseArgs(argv) {
  const found = Object.create(null);
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (!key.startsWith("--") || !(key in {
      "--origin": 1, "--revision": 1, "--stage": 1, "--seconds": 1,
      "--output": 1, "--browser": 1, "--apply": 1
    })) throw new Error("INVALID_ARGUMENT");
    if (key === "--apply") {
      if (found.apply) throw new Error("DUPLICATE_ARGUMENT");
      found.apply = true;
      continue;
    }
    const value = argv[++i];
    if (!value || value.startsWith("--") || found[key.slice(2)] !== undefined)
      throw new Error("INVALID_OR_DUPLICATE_VALUE");
    found[key.slice(2)] = value;
  }
  const origin = found.origin;
  const parsed = new URL(origin);
  if (parsed.protocol !== "https:" || !ALLOWED.has(parsed.hostname) ||
      parsed.username || parsed.password || parsed.pathname !== "/" ||
      parsed.search || parsed.hash || parsed.port || origin !== parsed.origin)
    throw new Error("STAGING_HTTPS_ORIGIN_REQUIRED");
  if (!SHA40.test(found.revision ?? "")) throw new Error("EXACT_RUNTIME_SHA_REQUIRED");
  const stage = Number(found.stage);
  const seconds = Number(found.seconds ?? "45");
  if (!Number.isInteger(stage) || !LEVELS.includes(stage)) throw new Error("INVALID_STAGE");
  if (!Number.isInteger(seconds) || seconds < 30 || seconds > 180)
    throw new Error("INVALID_DURATION");
  if (found.apply && !found.output) throw new Error("EXCLUSIVE_OUTPUT_REQUIRED");
  return { origin, revision: found.revision, stage, seconds,
           output: found.output, browser: found.browser, apply: !!found.apply };
}

export function documentNavigationOK(status, loadedBefore) {
  // HTTP 304 is a normal conditional browser-cache response, but only after
  // the same context actually loaded the document with HTTP 200.
  return status === 200 || (status === 304 && loadedBefore);
}

export function quantiles(values) {
  if (!values.length) return null;
  const copy = [...values].sort((a, b) => a - b);
  return Object.fromEntries([
    ["p50", 0.50], ["p95", 0.95], ["p99", 0.99]
  ].map(([key, v]) => [key, Math.round(copy[Math.ceil(copy.length * v) - 1])]));
}

export function gate(stats) {
  const total = stats.navigationTotal;
  const failed = stats.navigationFailed;
  if (stats.abortReason) return "STOP_" + stats.abortReason;
  if (stats.healthFailures > 0) return "FAIL_HEALTH";
  if (stats.http429 > 0 || stats.http5xx > 0) return "FAIL_GATEWAY";
  if (total < stats.users || failed > Math.max(1, Math.floor(total * 0.02)))
    return "FAIL_NAVIGATION";
  const lat = quantiles(stats.latencies);
  if (!lat || lat.p95 > 5000 || lat.p99 > 10000) return "FAIL_LATENCY";
  return "PASS";
}

export async function ensureRenderedTitle(page) {
  let title = await page.title();
  if (!title.includes("ASA")) {
    try {
      await page.waitForFunction(() => document.title.includes("ASA"),
        { timeout: 2500, polling: 100 });
    } catch { throw new Error("TITLE_NOT_READY"); }
    title = await page.title();
    if (!title.includes("ASA")) throw new Error("TITLE_NOT_READY");
  }
}

function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }
function failName(error) { return (error && error.name || "UNKNOWN").replace(/[^A-Za-z0-9]/g, "").slice(0, 55); }

export async function checkRuntime(origin, revision) {
  for (const [route, what] of [["/health/ready", "health"], ["/build-metadata.json", "revision"]]) {
    const response = await fetch(origin + route, {
      signal: AbortSignal.timeout(5000), redirect: "manual", cache: "no-store"
    });
    if (response.status !== 200) throw new Error("CHECK_" + what.toUpperCase() + "_HTTP");
    const body = await response.json();
    if (what === "health" && body.status !== "ready") throw new Error("NOT_READY");
    if (what === "revision" && body.revision !== revision) throw new Error("RUNTIME_REVISION_DRIFT");
  }
}

async function execute(config) {
  const fromCwd = createRequire(path.join(process.cwd(), "package.json"));
  // Puppeteer 25.16.0 is the separately pinned browser runner dependency.
  let puppeteer;
  try { puppeteer = fromCwd("puppeteer"); }
  catch { throw new Error("PUPPETEER_NOT_INSTALLED_IN_WORKING_DIRECTORY"); }
  const browserPath = config.browser || process.env.ASA_CHROME_PATH ||
    (await puppeteer.executablePath());
  if (!browserPath || !fs.existsSync(browserPath)) throw new Error("CHROME_EXECUTABLE_MISSING");
  await checkRuntime(config.origin, config.revision);

  const stats = {
    users: config.stage, navigationTotal: 0, navigationFailed: 0,
    http429: 0, http5xx: 0, healthFailures: 0, jsExceptions: 0,
    firstPartyResourceFailures: 0, thirdPartyBlocked: 0,
    latencies: [], cached304: 0, byScenario: Object.fromEntries(SCENARIOS.map(s => [s.id, { ok: 0, failed: 0, latency: [], failureCodes: {} }])),
    abortReason: null, peakOpenPages: 0, generatorMaxRssMiB: 0,
  };
  const originHost = new URL(config.origin).host;
  let open = 0;
  let healthConsecutive = 0;
  let monitoring = false;
  let browser = null;
  const begin = performance.now();
  const end = begin + config.seconds * 1000;

  function fail(code) {
    if (!stats.abortReason) stats.abortReason = code;
  }

  async function healthPulse() {
    if (monitoring) return;
    monitoring = true;
    try {
      await checkRuntime(config.origin, config.revision);
      healthConsecutive = 0;
    } catch (e) {
      stats.healthFailures++;
      healthConsecutive++;
      if (healthConsecutive >= 2 || e.message === "RUNTIME_REVISION_DRIFT")
        fail("SERVER_HEALTH_OR_REVISION");
    } finally { monitoring = false; }
    stats.generatorMaxRssMiB = Math.max(stats.generatorMaxRssMiB,
      Math.round(process.memoryUsage().rss / 1048576));
  }

  try {
    browser = await puppeteer.launch({ headless: true, executablePath: browserPath,
      args: ["--disable-background-timer-throttling", "--disable-renderer-backgrounding"] });
    const interval = setInterval(healthPulse, 4000);
    try {
      async function virtualUser(index) {
        let context;
        let page;
        try {
          await sleep(index * 120); // bounded ramp: 100 contexts over ~12 seconds
          if (stats.abortReason || performance.now() >= end) return;
          context = await browser.createBrowserContext();
          page = await context.newPage();
          open++;
          stats.peakOpenPages = Math.max(open, stats.peakOpenPages);
          await page.setViewport({ width: 1200, height: 750 });
          page.setDefaultNavigationTimeout(15000);
          await page.setRequestInterception(true);
          page.on("request", req => {
            try {
              if (new URL(req.url()).host !== originHost) {
                stats.thirdPartyBlocked++;
                void req.abort("blockedbyclient");
              } else void req.continue();
            } catch { void req.abort("blockedbyclient"); }
          });
          page.on("response", response => {
            if (!response.url().startsWith(config.origin + "/")) return;
            if (response.status() === 429) { stats.http429++; fail("HTTP_429"); }
            if (response.status() >= 500) { stats.http5xx++; fail("HTTP_5XX"); }
          });
          page.on("pageerror", () => { stats.jsExceptions++; });
          page.on("requestfailed", req => {
            try { if (new URL(req.url()).host === originHost) stats.firstPartyResourceFailures++; }
            catch { /* ignore */ }
          });
          const previouslyLoaded = new Set();
          let step = index % SCENARIOS.length;
          while (performance.now() < end && !stats.abortReason) {
            const scenario = SCENARIOS[step % SCENARIOS.length];
            step++;
            const started = performance.now();
            try {
              const response = await page.goto(config.origin + scenario.path,
                { waitUntil: "domcontentloaded", timeout: 15000 });
              const ms = performance.now() - started;
              if (!response || !documentNavigationOK(response.status(), previouslyLoaded.has(scenario.path)))
                throw new Error("DOCUMENT_NON_200");
              if (response.status() === 304) stats.cached304++;
              if (response.status() === 200) previouslyLoaded.add(scenario.path);
              await ensureRenderedTitle(page);
              // Real browser render/scroll, not authorized project editing.
              await page.evaluate(() => window.scrollTo(0, Math.min(document.body.scrollHeight, 350)));
              stats.latencies.push(ms);
              stats.navigationTotal++;
              stats.byScenario[scenario.id].latency.push(ms);
              stats.byScenario[scenario.id].ok++;
            } catch (e) {
              stats.navigationFailed++;
              stats.byScenario[scenario.id].failed++;
              const safe = e.message === "TITLE_NOT_READY" || e.message === "DOCUMENT_NON_200"
                ? e.message : "NAVIGATION_" + failName(e);
              stats.byScenario[scenario.id].failureCodes[safe] =
                (stats.byScenario[scenario.id].failureCodes[safe] || 0) + 1;
              if (stats.navigationFailed > 1) fail("NAVIGATION_FAILURE");
            }
            await sleep(750 + (index % 7) * 110);
          }
        } catch (e) {
          stats.navigationFailed++;
          fail("BROWSER_CONTEXT_" + failName(e));
        } finally {
          if (context) await context.close().catch(() => {});
          if (page) open--;
        }
      }
      await Promise.all(Array.from({ length: config.stage }, (_, i) => virtualUser(i)));
    } finally { clearInterval(interval); }
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  await checkRuntime(config.origin, config.revision).catch(() => {
    stats.healthFailures++;fail("POST_RUN_HEALTH");
  });
  const verdict = gate(stats);
  return {
    format: "asa-school-browser-load-v1", runAt: new Date().toISOString(),
    origin: config.origin, revision: config.revision, stage: config.stage,
    requestedSeconds: config.seconds, observedSeconds: +( (performance.now()-begin) / 1000).toFixed(1),
    generator: os.hostname(), device: "external-browser-generator",
    authenticatedPupilSessions: 0, actualEditorOpened: false,
    coveredUserActions: ["public pages", "3d/electronics/blocks product information", "project sign-in shell", "scroll"],
    excludes: ["authenticated Scratch editing", "3D WebGL", "electronics simulation", "project writes", "database persistence"],
    navigationCount: stats.navigationTotal, navigationErrors: stats.navigationFailed,
    cachedDocument304: stats.cached304,
    request429: stats.http429, request5xx: stats.http5xx,
    javascriptExceptions: stats.jsExceptions, firstPartyResourceFailures: stats.firstPartyResourceFailures,
    blockedThirdPartyRequests: stats.thirdPartyBlocked,
    healthFailures: stats.healthFailures, navigationLatencyMs: quantiles(stats.latencies),
    perScenario: Object.fromEntries(Object.entries(stats.byScenario).map(([name, s]) =>
      [name, { successes: s.ok, errors: s.failed, failureCodes: s.failureCodes, latencyMs: quantiles(s.latency) }])),
    generatorMaxNodeRssMiB: stats.generatorMaxRssMiB,
    peakOpenBrowserPages: stats.peakOpenPages, verdict, stopReason: stats.abortReason,
  };
}

export async function main(argv = process.argv.slice(2)) {
  let args;
  try { args = parseArgs(argv); }
  catch (e) { console.error("BLOCKED_" + failName(e)); return 2; }
  if (!args.apply) {
    console.log(JSON.stringify({ mode: "PLAN_ONLY", networkRequests: 0, browserLaunches: 0,
      levels: LEVELS, stage: args.stage, seconds: args.seconds, origin: args.origin,
      coverage: "anonymous public Chrome browsing, no project edits" }));
    return 0;
  }
  if (fs.existsSync(args.output)) {
    console.error("BLOCKED_OUTPUT_EXISTS"); return 2;
  }
  let report;
  try { report = await execute(args); }
  catch (error) {
    console.error("BROWSER_LOAD_BLOCKED_" + failName(error));
    return 2;
  }
  try {
    const text = JSON.stringify(report, null, 2) + "\n";
    fs.writeFileSync(args.output, text, { flag: "wx", mode: 0o600 });
  } catch { console.error("BLOCKED_REPORT_WRITE"); return 2; }
  console.log(JSON.stringify({ result: report.verdict, stage: report.stage,
    observedSeconds: report.observedSeconds, navigations: report.navigationCount,
    p95ms: report.navigationLatencyMs?.p95, http429: report.request429, http5xx: report.request5xx,
    jsExceptions: report.javascriptExceptions, peakOpenBrowserPages: report.peakOpenBrowserPages,
    reportFile: args.output }));
  return report.verdict === "PASS" ? 0 : 1;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().then(code => { process.exitCode = code; });
}
