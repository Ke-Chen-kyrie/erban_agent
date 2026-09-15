const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class ClassList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach(name => this.values.add(name)); }
  remove(...names) { names.forEach(name => this.values.delete(name)); }
  toggle(name, force) {
    if (force === true) this.values.add(name);
    else if (force === false) this.values.delete(name);
    else if (this.values.has(name)) this.values.delete(name);
    else this.values.add(name);
  }
  contains(name) { return this.values.has(name); }
}

class FakeElement {
  constructor(id = "") {
    this.id = id;
    this.checked = false;
    this.classList = new ClassList();
    this.listeners = {};
    this.src = "";
    this.textContent = "";
    this.title = "";
  }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  append() {}
  replaceChildren() {}
  setAttribute() {}
  querySelector() { return new FakeElement(); }
}

async function flush() {
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
}

async function main() {
  const scenario = process.argv[2];
  const html = fs.readFileSync(process.argv[3], "utf8");
  const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
  const script = scripts.at(-1)?.[1];
  assert.ok(script, "dashboard must contain an executable inline script");
  const toggleMarkup = html.match(/<input\b[^>]*\bid=["']attentionToggle["'][^>]*>/i)?.[0];
  assert.ok(toggleMarkup, "dashboard must render the attention toggle");
  assert.ok(!/\bchecked\b/i.test(toggleMarkup), "attention toggle must render unchecked");
  assert.match(html, /<img\b[^>]*\bid=["']attentionOverlay["'][^>]*>/i);

  const elements = {};
  const getElement = id => elements[id] || (elements[id] = new FakeElement(id));
  if (scenario === "restored-form") getElement("attentionToggle").checked = true;
  const timers = [];
  const requests = [];
  const pendingImages = [];
  const abortControllers = [];
  class FakeAbortController {
    constructor() {
      const listeners = [];
      this.signal = {
        aborted: false,
        addEventListener: (type, listener) => { if (type === "abort") listeners.push(listener); },
      };
      this.abort = () => {
        this.signal.aborted = true;
        listeners.forEach(listener => listener());
      };
      abortControllers.push(this);
    }
  }
  class FakeImage {
    constructor() { pendingImages.push(this); }
    set src(value) { this._src = value; }
    get src() { return this._src; }
  }
  const context = {
    addEventListener() {},
    AbortController: FakeAbortController,
    clearInterval() {},
    clearTimeout() {},
    console,
    document: {
      body: { classList: new ClassList() },
      createElement: () => new FakeElement(),
      documentElement: {},
      fullscreenElement: null,
      getElementById: getElement,
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener() {},
    },
    fetch: async (url, options = {}) => {
      requests.push(url);
      if (url === "/api/processes") return { ok: true, json: async () => ({ processes: [] }) };
      if (scenario === "abort-request" && url === "/api/attention/queue") {
        return new Promise((resolve, reject) => {
          options.signal?.addEventListener("abort", () => {
            const error = new Error("aborted"); error.name = "AbortError"; reject(error);
          });
        });
      }
      if (url === "/api/attention/queue") return { ok: true, json: async () => ({ queue_size: 1 }) };
      if (url === "/api/attention/pop") return {
        ok: true,
        json: async () => ({
          rendered: scenario === "data-url"
            ? { image_base64: "data:image/png;base64,cG5n" }
            : scenario === "declared-mime"
              ? { image_base64: "cG5n", image_mime_type: "image/webp" }
              : { image_base64: "cG5n" },
        }),
      };
      throw new Error(`unexpected request: ${url}`);
    },
    location: { host: "dashboard.test", protocol: "http:", search: "" },
    Image: FakeImage,
    setInterval: () => 1,
    setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    WebSocket: class { constructor() {} },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(script, context, { filename: "dashboard.html" });
  await flush();

  const toggle = getElement("attentionToggle");
  const overlay = getElement("attentionOverlay");
  assert.equal(toggle.checked, false, "attention must start disabled");
  assert.ok(!requests.includes("/api/attention/queue"), "disabled attention must not consume the queue");
  if (scenario === "restored-form") return;

  toggle.checked = true;
  const enabling = toggle.listeners.change({ target: toggle });
  await flush();

  if (scenario === "abort-request") {
    assert.equal(abortControllers.length, 1, "polling must own an abort controller");
    toggle.checked = false;
    await toggle.listeners.change({ target: toggle });
    assert.equal(abortControllers[0].signal.aborted, true, "disabling must abort the request");
    await enabling;
    return;
  }
  await enabling;

  assert.deepEqual(
    requests.filter(url => url.startsWith("/api/attention/")),
    ["/api/attention/queue", "/api/attention/pop"],
  );
  const expectedSource = scenario === "data-url"
    ? "data:image/png;base64,cG5n"
    : scenario === "declared-mime"
      ? "data:image/webp;base64,cG5n"
      : "data:image/jpeg;base64,cG5n";
  assert.equal(pendingImages.length, 1, "result must be decoded before display");
  assert.equal(pendingImages[0].src, expectedSource);
  assert.equal(overlay.classList.contains("visible"), false, "loading image must not mask live video");
  if (scenario === "image-error") {
    pendingImages[0].onerror();
    await flush();
    assert.equal(overlay.classList.contains("visible"), false);
    assert.ok(!timers.some(timer => timer.delay === 3000));
    return;
  }
  pendingImages[0].onload();
  assert.equal(overlay.src, expectedSource);
  assert.equal(overlay.classList.contains("visible"), true);
  const holdTimer = timers.find(timer => timer.delay === 3000);
  assert.ok(holdTimer, "attention overlay must use a fixed three-second hold");

  holdTimer.callback();
  assert.equal(overlay.classList.contains("visible"), false);
}

main().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
