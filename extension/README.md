# Save to Recrute (Chrome MV3 extension)

Passive capture (PLAN.md §3.2, Tier 3 mode 1): while *you* browse a job page (LinkedIn, Indeed,
a company careers page, an ATS board...), click the toolbar button or right-click → **Save job to
Recrute**. The extension sends that one page's URL, title and HTML to your Recrute server, which
turns it into a job (`recrute.capture.page.raw_job_from_capture`).

It does nothing on its own: no background crawling, no page reads without a click, and no
requests to the job site.

## Load unpacked

1. Open `chrome://extensions` (Edge: `edge://extensions`).
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select this `extension/` folder.
4. Open the extension's **Options** (Details → Extension options, or right-click the toolbar
   icon → Options):
   - **Server capture URL**: default `http://127.0.0.1:8765/api/capture`. For the Windows-laptop
     server on your LAN use e.g. `http://laptop.local:8765/api/capture`; Chrome asks once for
     permission to reach that host.
   - **Token**: the capture token configured on the Recrute server.
5. Pin the extension, open a job page, click the icon.

After editing the files, click the reload icon on the extension's card in `chrome://extensions`.

## Permissions (minimal)

| Permission | Why |
|---|---|
| `activeTab` + `scripting` | read `document.documentElement.outerHTML` of the current tab, only after you click |
| `storage` | server URL and token (`chrome.storage.local`, not synced) |
| `contextMenus` | the right-click "Save job to Recrute" item |
| host `http://127.0.0.1/*`, `http://localhost/*` | reach a local server (any port) |
| optional host `http(s)://*/*` | requested at runtime **only for the one server host you configure** |

## Badge

| Badge | Meaning |
|---|---|
| `NEW` (green) | saved as a new job |
| `OK` (blue) | already known; this capture was merged into the existing job |
| `AUTH` | server answered 401: token wrong |
| `SET` | server URL/token not configured or host permission missing; Options opens |
| `OFF` | server unreachable |
| `ERR` | page can't be read (e.g. `chrome://` pages, the Web Store) or the server rejected it |

Hover the icon for details.

## Server contract (implemented by the Recrute web app)

```
POST <server capture URL>            (default http://127.0.0.1:8765/api/capture)
Content-Type: application/json
X-Recrute-Token: <token>

{"url": "https://www.linkedin.com/jobs/view/4012345678/",
 "title": "Security Analyst | Acme | LinkedIn",
 "html": "<html>...</html>"}          # the page without scripts/styles/media, form controls,
                                       # non-descriptive meta tags or session data (JSON-LD kept);
                                       # if the request would exceed 5,000,000 bytes, only the
                                       # head's metadata + main job content; else not sent
```

Responses (JSON):

| Status | Body | When |
|---|---|---|
| 200 | `{"ok": true, "job_id": 123, "new": true}` | job created |
| 200 | `{"ok": true, "job_id": 123, "new": false}` | job already known (sources merged) |
| 401 | `{"ok": false, "error": "bad token"}` | missing/wrong `X-Recrute-Token` |
| 422 | `{"ok": false, "error": "not a job page"}` | `raw_job_from_capture` returned `None` |
| 413 | `{"ok": false, "error": "too large"}` | body over the server's limit |

Server-side notes for the integrator:
- Compare the token with `hmac.compare_digest`; keep the token in the keyring or config, never in
  the DB.
- The extension's service worker fetches with host permission, so no CORS headers are needed.
  Do **not** add permissive CORS (`*`) to this endpoint: that would let any website post to it.
- The server may be reached over plain HTTP on your LAN; the token is sent in clear there. Keep
  the server on a trusted network.
