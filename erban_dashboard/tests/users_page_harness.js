const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class FakeElement {
  constructor() {
    this.children = [];
    this.className = "";
    this.disabled = false;
    this.listeners = {};
    this.src = "";
    this.style = {};
    this.textContent = "";
    this.value = "";
  }

  addEventListener(type, listener) { this.listeners[type] = listener; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
}

function jsonResponse(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data };
}

async function main() {
  const scenario = process.argv[2];
  const html = fs.readFileSync(process.argv[3], "utf8");
  const match = html.match(/<script>([\s\S]*)<\/script>/);
  assert.ok(match, "users page must contain an executable script");

  const ids = [
    "status", "userTable", "tableEmpty", "userCount", "searchInput", "faceImg",
    "facePlaceholder", "detailBody", "deleteBtn", "refreshBtn", "syncBtn", "clearBtn",
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new FakeElement()]));
  const pendingFaces = new Map();
  const context = {
    console,
    confirm: () => true,
    document: {
      createElement: () => new FakeElement(),
      getElementById: id => elements[id] || (elements[id] = new FakeElement()),
    },
    encodeURIComponent,
    fetch: async (url, options = {}) => {
      if (url === "/api/identity/users") return jsonResponse({ total: 0, users: [] });
      if (url.endsWith("/face")) {
        return new Promise(resolve => pendingFaces.set(url, resolve));
      }
      if (options.method === "DELETE") {
        return jsonResponse({ user_id: "missing", deleted: false, message: "User not found" });
      }
      if (url === "/api/identity/sync") {
        return jsonResponse({ voice_cleanup: [], face_cleanup: [] });
      }
      throw new Error(`unexpected request: ${options.method || "GET"} ${url}`);
    },
  };
  vm.createContext(context);
  vm.runInContext(match[1], context, { filename: "users.html" });
  await new Promise(resolve => setImmediate(resolve));

  if (scenario === "face-race") {
    const first = context.selectUser({ user_id: "first", name: "甲", role: "老人" });
    const second = context.selectUser({ user_id: "second", name: "乙", role: "老人" });
    pendingFaces.get("/api/identity/users/second/face")(
      jsonResponse({ user_id: "second", face_image: "data:image/jpeg;base64,SECOND" })
    );
    await second;
    pendingFaces.get("/api/identity/users/first/face")(
      jsonResponse({ user_id: "first", face_image: "data:image/jpeg;base64,FIRST" })
    );
    await first;
    assert.equal(elements.faceImg.src, "data:image/jpeg;base64,SECOND");
    return;
  }

  if (scenario === "delete-false") {
    await context.deleteUser("missing");
    assert.match(elements.status.className, /err/);
    assert.match(elements.status.textContent, /User not found/);
    return;
  }

  throw new Error(`unknown scenario: ${scenario}`);
}

main().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
