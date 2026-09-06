#!/usr/bin/env node
"use strict";

const fs = require("fs");
const net = require("net");
const path = require("path");
const {spawn} = require("child_process");

const REPO = path.resolve(__dirname, "..");
const CHROME = process.env.CHROME || "/usr/bin/google-chrome";
const BASE_URL = process.env.GATE_BASE_URL || "http://127.0.0.1:5000";
const OUT_DIR = process.env.GATE_OUT_DIR || path.join(REPO, "QAQC", "Evidence", "launch-2026-08-21");
const ARTIFACT_DIR = process.env.GATE_ARTIFACT_DIR || "/opt/cursor/artifacts";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class CdpClient {
  constructor(wsUrl) {
    this.nextId = 1;
    this.pending = new Map();
    this.console = [];
    this.ws = new WebSocket(wsUrl);
    this.ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const {resolve, reject, timer} = this.pending.get(message.id);
        clearTimeout(timer);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(message.error.message));
        else resolve(message.result || {});
        return;
      }
      if (message.method === "Runtime.consoleAPICalled") {
        const args = (message.params.args || []).map((item) => item.value || item.description || "");
        this.console.push({type: message.params.type, text: args.join(" ")});
      }
    };
  }

  async open() {
    await new Promise((resolve, reject) => {
      this.ws.onopen = resolve;
      this.ws.onerror = reject;
    });
  }

  send(method, params = {}, timeoutMs = 30000) {
    const id = this.nextId++;
    this.ws.send(JSON.stringify({id, method, params}));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method} timed out`));
      }, timeoutMs);
      this.pending.set(id, {resolve, reject, timer});
    });
  }

  close() {
    this.ws.close();
  }
}

async function findOpenPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
    server.on("error", reject);
  });
}

async function waitForHttp(url, timeoutMs = 20000) {
  const started = Date.now();
  let lastError = null;
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
      lastError = new Error(`${url} ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await sleep(200);
  }
  throw lastError || new Error(`timeout ${url}`);
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || "evaluate failed");
  }
  return result.result ? result.result.value : undefined;
}

async function waitFor(cdp, expression, timeoutMs = 20000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const value = await evaluate(cdp, expression);
    if (value) return value;
    await sleep(200);
  }
  throw new Error(`timeout waiting for ${expression}`);
}

async function screenshot(cdp, name) {
  fs.mkdirSync(OUT_DIR, {recursive: true});
  fs.mkdirSync(ARTIFACT_DIR, {recursive: true});
  const result = await cdp.send("Page.captureScreenshot", {format: "png", fromSurface: true});
  const buffer = Buffer.from(result.data, "base64");
  const fileName = `${name}.png`;
  const evidencePath = path.join(OUT_DIR, fileName);
  const artifactPath = path.join(ARTIFACT_DIR, fileName);
  fs.writeFileSync(evidencePath, buffer);
  fs.writeFileSync(artifactPath, buffer);
  return {evidencePath, artifactPath};
}

async function main() {
  fs.mkdirSync(OUT_DIR, {recursive: true});
  const results = [];
  const debugPort = await findOpenPort();
  const userData = fs.mkdtempSync("/tmp/launch-chrome-");
  const chrome = spawn(
    CHROME,
    [
      `--remote-debugging-port=${debugPort}`,
      `--user-data-dir=${userData}`,
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--window-size=1440,900",
      "--hide-scrollbars",
      "about:blank",
    ],
    {stdio: "ignore"},
  );
  let cdp;
  try {
    await waitForHttp(`http://127.0.0.1:${debugPort}/json/version`);
    const target = await fetch(
      `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(BASE_URL + "/")}`,
      {method: "PUT"},
    ).then((response) => response.json());
    cdp = new CdpClient(target.webSocketDebuggerUrl);
    await cdp.open();
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await waitFor(cdp, "document.readyState === 'complete'");
    await waitFor(cdp, "!!document.getElementById('startup-overlay')");

    const overlayBefore = await evaluate(
      cdp,
      `(() => {
        const overlay = document.getElementById('startup-overlay');
        const closeBtn = overlay.querySelector('.startup-close');
        const cancel = Array.from(overlay.querySelectorAll('button')).find((btn) => /cancel/i.test(btn.textContent || ''));
        return {
          title: document.title,
          active: overlay.classList.contains('active'),
          role: overlay.getAttribute('role'),
          modal: overlay.getAttribute('aria-modal'),
          submit: (document.getElementById('startup-submit') || {}).textContent,
          hasClose: !!closeBtn,
          hasCancel: !!cancel,
          total: (document.getElementById('startup-total') || {}).textContent,
        };
      })()`,
    );
    await screenshot(cdp, "b01_startup_overlay");
    await evaluate(cdp, "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))");
    const overlayAfterEscape = await evaluate(
      cdp,
      `document.getElementById('startup-overlay').classList.contains('active')`,
    );
    const b01Pass = overlayBefore.active && overlayBefore.role === "dialog" && overlayAfterEscape && !overlayBefore.hasClose && !overlayBefore.hasCancel;
    results.push({
      id: "B01",
      status: b01Pass ? "PASS" : "FAIL",
      detail: {overlayBefore, overlayAfterEscape},
    });

    await evaluate(
      cdp,
      `(() => {
        for (const size of [500, 1000, 1500, 2000, 2500]) {
          const input = document.getElementById('startup-size-' + size);
          if (input) {
            input.value = '2';
            input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new Event('change', {bubbles: true}));
          }
        }
        document.getElementById('startup-submit').click();
      })()`,
    );
    await waitFor(
      cdp,
      `!document.getElementById('startup-overlay').classList.contains('active')`,
      30000,
    );
    await sleep(800);
    await screenshot(cdp, "b02_fleet_status");

    const tabs = [
      ["B02a", "status", "Fleet Status"],
      ["B02b", "subfleets", "Sub-Fleets"],
      ["B02c", "runbooks", "Scenario Timelines"],
      ["B02d", "modbus", "Modbus Registers"],
      ["B02e", "howto", "How To Use"],
      ["B02f", "scada", "One-Line SCADA tab"],
    ];
    const seen = new Set();
    for (const [id, tab, label] of tabs) {
      await evaluate(cdp, `typeof switchTab === 'function' && switchTab(${JSON.stringify(tab)})`);
      await sleep(400);
      const state = await evaluate(
        cdp,
        `(() => {
          const btn = document.querySelector('[data-tab="${tab}"]');
          const panel = document.getElementById('tab-${tab === "howto" ? "howto" : tab}');
          return {
            buttonText: btn ? btn.textContent.trim() : null,
            buttonActive: btn ? btn.classList.contains('active') : false,
            panelActive: panel ? panel.classList.contains('active') : false,
            panelText: panel ? panel.innerText.slice(0, 240) : null,
          };
        })()`,
      );
      const shotName = `b_${tab}`;
      if (!seen.has(tab)) {
        await screenshot(cdp, shotName);
        seen.add(tab);
      }
      const pass = Boolean(state.buttonActive && state.panelActive);
      results.push({id, title: label, status: pass ? "PASS" : "FAIL", detail: state});
    }

    await evaluate(cdp, `typeof switchTab === 'function' && switchTab('subfleets')`);
    await sleep(400);
    const builder = await evaluate(
      cdp,
      `(() => {
        const text = (document.getElementById('tab-subfleets') || {}).innerText || '';
        return {
          hasCreate: /create/i.test(text),
          preview: text.slice(0, 300),
        };
      })()`,
    );
    await screenshot(cdp, "b05_subfleets");
    results.push({
      id: "B05",
      status: builder.hasCreate || builder.preview.length > 20 ? "PASS" : "FAIL",
      detail: builder,
    });

    await evaluate(cdp, `typeof switchTab === 'function' && switchTab('howto')`);
    await sleep(400);
    const help = await evaluate(
      cdp,
      `(() => {
        const text = (document.getElementById('tab-howto') || {}).innerText || '';
        return {chars: text.length, preview: text.slice(0, 300)};
      })()`,
    );
    results.push({
      id: "B07",
      status: help.chars > 80 ? "PASS" : "FAIL",
      detail: help,
    });
    await sleep(600);
    const scada = await evaluate(
      cdp,
      `(() => {
        const tree = document.querySelector('.scada-tree-item, [class*="scada"] button, #tab-scada');
        const text = (document.getElementById('tab-scada') || {}).innerText || '';
        return {hasTree: !!tree, preview: text.slice(0, 400)};
      })()`,
    );
    await screenshot(cdp, "b04_scada");
    results.push({
      id: "B04",
      status: scada.preview.length > 40 ? "PASS" : "FAIL",
      detail: scada,
    });

    await evaluate(cdp, `typeof switchTab === 'function' && switchTab('status')`);
    await sleep(400);
    const table = await evaluate(
      cdp,
      `(() => {
        const rows = document.querySelectorAll('#tab-status table tbody tr, #fleet-table tbody tr, table tbody tr');
        if (rows.length) rows[0].click();
        const modal = document.querySelector('[role="dialog"]:not(#startup-overlay)');
        return {rowCount: rows.length, modalOpen: !!(modal && getComputedStyle(modal).display !== 'none')};
      })()`,
    );
    await screenshot(cdp, "b03_fleet_table");
    results.push({
      id: "B03",
      status: table.rowCount > 0 ? "PASS" : "FAIL",
      detail: table,
    });

    const header = await evaluate(
      cdp,
      `(() => {
        const labels = Array.from(document.querySelectorAll('button')).map((btn) => (btn.textContent || '').trim());
        return {
          transfer: labels.some((text) => /transfer mode all/i.test(text)),
          parallel: labels.some((text) => /parallel mode all/i.test(text)),
          utilityFail: labels.some((text) => /utility failure all/i.test(text)),
          restore: labels.some((text) => /restore utility all/i.test(text)),
          exportCsv: labels.some((text) => /export config csv/i.test(text)),
        };
      })()`,
    );
    results.push({
      id: "B06",
      status: header.transfer && header.parallel && header.utilityFail && header.restore && header.exportCsv ? "PASS" : "FAIL",
      detail: header,
    });

    const errors = cdp.console.filter((item) => item.type === "error");
    results.push({
      id: "B-console",
      status: errors.length ? "FAIL" : "PASS",
      detail: {errors, logCount: cdp.console.length},
    });
  } finally {
    if (cdp) cdp.close();
    chrome.kill("SIGTERM");
  }

  const payload = {
    baseUrl: BASE_URL,
    results,
    failed: results.filter((item) => item.status !== "PASS").map((item) => item.id),
  };
  const jsonPath = path.join(OUT_DIR, "browser-wave.json");
  fs.writeFileSync(jsonPath, JSON.stringify(payload, null, 2) + "\n");
  fs.writeFileSync(path.join(ARTIFACT_DIR, "browser-wave.json"), JSON.stringify(payload, null, 2) + "\n");
  for (const item of results) {
    console.log(`[${item.status}] ${item.id} ${item.title || ""}`.trim());
  }
  if (payload.failed.length) {
    console.error(`[fail] ${payload.failed.join(",")}`);
    process.exit(1);
  }
  console.log("[ok] browser wave completed");
}

main().catch((error) => {
  console.error(`[fail] ${error.stack || error}`);
  process.exit(1);
});
