# Erban Dashboard Home Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the root event dashboard with a three-card function home and move the unchanged event dashboard to `/dashboard`.

**Architecture:** Add a packaged `home.html` whose only responsibility is navigation, and store its path under a new aiohttp application key. Route `/` to the home handler and `/dashboard` to the existing dashboard handler; update all page navigation without changing API or WebSocket endpoints.

**Tech Stack:** Python 3.10+, aiohttp, static HTML/CSS/JavaScript, `unittest` with `aiohttp.test_utils`.

**Spec:** `docs/superpowers/specs/2026-08-27-home-navigation-design.md`

## Global Constraints

- Keep `/register` and `/users` behavior unchanged.
- Keep all identity, event, video, process-control, HTTP, and WebSocket interfaces unchanged.
- `/` must render the three-entry home; it must not redirect to `/dashboard`.
- `/dashboard` must render the event detection dashboard.
- A custom dashboard HTML path must affect `/dashboard` only.
- Preserve the existing dark mint-green visual language and provide responsive and keyboard-accessible navigation.

## File Structure

- Create `erban_dashboard_app/static/home.html`: self-contained three-card navigation page.
- Modify `erban_dashboard_app/server.py`: load the packaged home independently and expose `/dashboard`.
- Modify `erban_dashboard_app/static/dashboard.html`: remove identity-page embedding and add a home link.
- Modify `erban_dashboard_app/static/register.html`: change return-to-dashboard copy to return-to-home.
- Modify `erban_dashboard_app/static/users.html`: change return-to-dashboard copy to return-to-home.
- Modify `tests/test_identity_ui.py`: verify home, dashboard, navigation, and custom-dashboard isolation.
- Modify `README.md`: document the new browser entry points and public page route.

---

### Task 1: Route the home and dashboard independently

**Files:**
- Create: `erban_dashboard_app/static/home.html`
- Modify: `erban_dashboard_app/server.py:57-62, 1400-1413, 1583-1626`
- Test: `tests/test_identity_ui.py:36-85`

**Interfaces:**
- Consumes: packaged static files under `erban_dashboard_app/static/`; optional `create_app(html_path)` custom dashboard path.
- Produces: `GET / -> home.html`; `GET /dashboard -> dashboard.html`; `HOME_HTML_KEY: web.AppKey[Path]`; `handle_dashboard_page(request) -> web.FileResponse`.

- [ ] **Step 1: Replace the old root-link test with failing route tests**

Add tests that assert the three exact destinations and custom-dashboard isolation:

```python
async def test_home_links_to_all_three_function_pages(self) -> None:
    response = await self.client.get("/")
    html = await response.text()
    self.assertEqual(response.status, 200)
    for href, label in (
        ('href="/register"', "用户注册"),
        ('href="/users"', "用户管理"),
        ('href="/dashboard"', "事件检测"),
    ):
        self.assertIn(href, html)
        self.assertIn(label, html)

async def test_dashboard_has_its_own_route(self) -> None:
    response = await self.client.get("/dashboard")
    self.assertEqual(response.status, 200)
    self.assertIn("REAL-TIME CARE CENTER", await response.text())
```

Update `test_custom_dashboard_html_does_not_require_custom_identity_pages` so it expects the packaged home at `/` and `custom dashboard` at `/dashboard`:

```python
home = await custom_client.get("/")
self.assertIn("用户注册", await home.text())
dashboard = await custom_client.get("/dashboard")
self.assertIn("custom dashboard", await dashboard.text())
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `uv run python -m unittest tests.test_identity_ui.IdentityPageTest -v`

Expected: FAIL because `/` still returns the dashboard and `/dashboard` returns 404.

- [ ] **Step 3: Add the minimal home page and server routing**

Create `home.html` with three full-card anchors:

```html
<main class="cards" aria-label="功能入口">
  <a class="card" href="/register"><h2>用户注册</h2><p>采集基本资料、人脸与声纹信息</p><span>进入功能 →</span></a>
  <a class="card" href="/users"><h2>用户管理</h2><p>查询和维护已登记的用户资料</p><span>进入功能 →</span></a>
  <a class="card" href="/dashboard"><h2>事件检测</h2><p>查看实时画面、异常事件与 Agent 对话</p><span>进入功能 →</span></a>
</main>
```

Complete it as a valid self-contained HTML document using the existing color variables, inline SVG icons, a three-column CSS grid, visible `:focus-visible` styles, hover feedback, and `@media (max-width: 760px) { .cards { grid-template-columns: 1fr; } }`.

In `server.py`, add `HOME_HTML_KEY`, load `STATIC_DIR / "home.html"`, validate it with `is_file()`, store it on the app, and split the handlers:

```python
async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(request.app[HOME_HTML_KEY])

async def handle_dashboard_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(request.app[DASHBOARD_HTML_KEY])
```

Register both routes:

```python
app.router.add_get("/", handle_index)
app.router.add_get("/dashboard", handle_dashboard_page)
```

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `uv run python -m unittest tests.test_identity_ui.IdentityPageTest -v`

Expected: all `IdentityPageTest` tests PASS.

- [ ] **Step 5: Commit the route and home page**

```bash
git add erban_dashboard_app/static/home.html erban_dashboard_app/server.py tests/test_identity_ui.py
git commit -m "feat: add dashboard function home"
```

If Git remains unavailable because the workspace `.git` metadata is absent, record that fact and continue without committing.

---

### Task 2: Detach identity navigation from the event dashboard

**Files:**
- Modify: `erban_dashboard_app/static/dashboard.html:50-85, 210-250, 540-565`
- Modify: `erban_dashboard_app/static/register.html`
- Modify: `erban_dashboard_app/static/users.html`
- Test: `tests/test_identity_ui.py`

**Interfaces:**
- Consumes: the routes `/`, `/register`, `/users`, and `/dashboard` created or preserved by Task 1.
- Produces: event dashboard navigation that only returns home; identity-page navigation whose return destination and copy are `/` and `返回首页`.

- [ ] **Step 1: Write failing navigation assertions**

Add this test:

```python
async def test_function_pages_return_home_and_dashboard_is_detached(self) -> None:
    dashboard = await (await self.client.get("/dashboard")).text()
    self.assertIn('href="/"', dashboard)
    self.assertNotIn('href="/register"', dashboard)
    self.assertNotIn('href="/users"', dashboard)

    for path in ("/register", "/users"):
        html = await (await self.client.get(path)).text()
        self.assertIn('href="/"', html)
        self.assertIn("返回首页", html)
        self.assertNotIn("返回大屏", html)
```

- [ ] **Step 2: Run the navigation test and verify it fails**

Run: `uv run python -m unittest tests.test_identity_ui.IdentityPageTest.test_function_pages_return_home_and_dashboard_is_detached -v`

Expected: FAIL because the dashboard still links to identity pages and identity pages still say `返回大屏`.

- [ ] **Step 3: Update page navigation with minimal markup changes**

In `dashboard.html`, make the brand a semantic home anchor, remove `.identity-nav` markup and its iframe, remove the `.identity-nav` click/message script, and delete styles used only by `.identity-nav`, `.identity-link`, and `#pageFrame`. Preserve fullscreen and launch controls unchanged.

In `register.html` and `users.html`, keep the existing `href="/"` destinations but replace visible `返回大屏` with `返回首页`. Remove the end-of-file iframe-only `window.top !== window.self` message bridge from both pages because the dashboard no longer embeds them.

- [ ] **Step 4: Run all identity UI tests**

Run: `uv run python -m unittest tests.test_identity_ui -v`

Expected: all tests PASS, including registration wizard and identity proxy tests.

- [ ] **Step 5: Commit the navigation cleanup**

```bash
git add erban_dashboard_app/static/dashboard.html erban_dashboard_app/static/register.html erban_dashboard_app/static/users.html tests/test_identity_ui.py
git commit -m "refactor: detach identity pages from event dashboard"
```

If Git remains unavailable because the workspace `.git` metadata is absent, record that fact and continue without committing.

---

### Task 3: Document and verify the finished route change

**Files:**
- Modify: `README.md:1-30, 95-115`
- Test: `tests/test_identity_ui.py`

**Interfaces:**
- Consumes: final browser route behavior from Tasks 1 and 2.
- Produces: user-facing documentation stating `/` is the function home and `/dashboard` is the event detection dashboard.

- [ ] **Step 1: Update README entry-point documentation**

Replace the startup paragraph with explicit route descriptions:

```markdown
浏览器访问 `http://127.0.0.1:8770` 进入功能首页。首页提供“用户注册”“用户管理”“事件检测”三个入口；也可以分别直接访问 `/register`、`/users` 和 `/dashboard`。
```

In the compatible-interface list, document `GET /` as the function home and add `GET /dashboard` as the event detection page while retaining the existing `/register` and `/users` entry.

- [ ] **Step 2: Run the complete automated test suite**

Run: `uv run python -m unittest discover -s tests -v`

Expected: all tests PASS with no failures or errors.

- [ ] **Step 3: Verify static packaging contains the new page**

Run: `uv build`

Expected: wheel and source distribution build successfully. Inspect with:

```bash
unzip -l dist/erban_dashboard-0.1.0-py3-none-any.whl | grep 'static/home.html'
```

Expected: one packaged `erban_dashboard_app/static/home.html` entry. The existing `static/*.html` package-data pattern requires no configuration change.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md
git commit -m "docs: describe dashboard home routes"
```

If Git remains unavailable because the workspace `.git` metadata is absent, report the uncommitted state in the handoff.
