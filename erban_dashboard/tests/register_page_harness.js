const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class FakeElement {
  constructor() {
    this.className = "";
    this.disabled = false;
    this.listeners = {};
    this.style = {};
    this.textContent = "";
    this.value = "";
  }

  addEventListener(type, listener) { this.listeners[type] = listener; }
}

async function main() {
  const html = fs.readFileSync(process.argv[2], "utf8");
  const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
  const script = scripts.at(-1)?.[1];
  assert.ok(script, "registration page must contain an executable inline script");

  const ids = [
    "userId", "userName", "userRole", "userDesc", "summaryName", "summaryRole",
    "summaryPhoto", "submitStatus", "backBtn", "nextBtn", "submitBtn",
    "finishActions", "stepActions", "cameraPreview", "photoPreview", "cameraEmpty",
    "cameraBtn", "captureBtn", "cameraState", "audioState", "recProgress",
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new FakeElement()]));
  elements.userRole.value = "老人";
  let submittedForm = null;
  const context = {
    Blob,
    FormData,
    URL,
    clearInterval,
    clearTimeout,
    console,
    document: {
      body: { classList: { add() {} } },
      getElementById: id => elements[id] || (elements[id] = new FakeElement()),
      querySelectorAll: () => [],
    },
    fetch: async (url, options = {}) => {
      if (url === "/api/identity/users") {
        return { ok: true, json: async () => ({ users: [] }) };
      }
      if (url === "/api/identity/users/register") {
        submittedForm = options.body;
        return { ok: true, json: async () => ({ name: "Alice" }) };
      }
      throw new Error(`unexpected request: ${url}`);
    },
    location: { search: "" },
    setInterval,
    setTimeout,
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(
    `${script}\nglobalThis.prepareSubmission=()=>{photoBlob=new Blob(["face"]);audioBlob=new Blob(["voice"]);audioKnown=true;audioDuration=12;};`,
    context,
    { filename: "register.html" },
  );
  await new Promise(resolve => setImmediate(resolve));

  elements.userName.value = "Alice";
  elements.userName.listeners.input();
  assert.equal(elements.userId.value, "elder_alice");
  context.prepareSubmission();
  await elements.submitBtn.onclick();

  assert.ok(submittedForm instanceof FormData);
  assert.equal(submittedForm.get("user_id"), "elder_alice");
}

main().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
