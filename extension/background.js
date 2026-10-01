// "Save to Recrute" service worker: toolbar button + context menu -> POST the current page's
// {url, title, html} to the Recrute server. Only runs on an explicit user gesture (activeTab).

import { getSettings, originPattern } from "./common.js";

const MENU_ID = "save-to-recrute";
// The server's limit for the whole request body (MAX_CAPTURE_BYTES in web/views_apps.py),
// measured in UTF-8 bytes of the serialized JSON.
const MAX_REQUEST_BYTES = 5_000_000;

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: MENU_ID,
    title: "Save job to Recrute",
    contexts: ["page", "link", "selection"],
  });
});

chrome.action.onClicked.addListener((tab) => saveTab(tab));

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === MENU_ID && tab) saveTab(tab);
});

async function setBadge(tabId, text, color, title) {
  try {
    await chrome.action.setBadgeBackgroundColor({ tabId, color });
    await chrome.action.setBadgeText({ tabId, text });
    await chrome.action.setTitle({ tabId, title: `Save to Recrute: ${title}` });
  } catch (_) {
    // tab closed meanwhile
  }
  // Clear the badge after a while so it doesn't look stale.
  setTimeout(() => {
    chrome.action.setBadgeText({ tabId, text: "" }).catch(() => {});
  }, 8000);
}

async function capturePage(tabId) {
  // capture.js defines recruteCapture() in the page (see there for what is and isn't sent)
  await chrome.scripting.executeScript({ target: { tabId }, files: ["capture.js"] });
  const [result] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => globalThis.recruteCapture(),
  });
  return result && result.result;
}

// The first candidate whose serialized request fits the server's byte limit, else null.
function fitRequest(page) {
  const enc = new TextEncoder();
  for (const html of page.candidates || []) {
    const body = JSON.stringify({ url: page.url, title: page.title, html });
    if (enc.encode(body).length <= MAX_REQUEST_BYTES) return body;
  }
  return null;
}

async function saveTab(tab) {
  const tabId = tab.id;
  if (!tab.url || !/^https?:/i.test(tab.url)) {
    await setBadge(tabId, "ERR", "#b00020", "only http(s) pages can be saved");
    return;
  }
  const settings = await getSettings();
  if (!settings.token) {
    await setBadge(tabId, "SET", "#b26a00", "set the server URL and token in Options");
    chrome.runtime.openOptionsPage();
    return;
  }
  const hasHost = await chrome.permissions.contains({
    origins: [originPattern(settings.serverUrl)],
  });
  if (!hasHost) {
    await setBadge(tabId, "SET", "#b26a00", "grant access to the server in Options");
    chrome.runtime.openOptionsPage();
    return;
  }

  await setBadge(tabId, "…", "#555555", "saving…");
  let page;
  try {
    page = await capturePage(tabId);
  } catch (e) {
    await setBadge(tabId, "ERR", "#b00020", `cannot read this page (${e.message || e})`);
    return;
  }
  if (!page) {
    await setBadge(tabId, "ERR", "#b00020", "cannot read this page");
    return;
  }
  const body = fitRequest(page);
  if (body === null) {
    await setBadge(tabId, "BIG", "#b00020", "page too large to save, even reduced to the job content");
    return;
  }

  let resp;
  try {
    resp = await fetch(settings.serverUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Recrute-Token": settings.token,
      },
      body,
    });
  } catch (e) {
    await setBadge(tabId, "OFF", "#b00020", `server unreachable (${settings.serverUrl})`);
    return;
  }

  let data = {};
  try {
    data = await resp.json();
  } catch (_) {
    // non-JSON error page
  }
  if (resp.status === 401) {
    await setBadge(tabId, "AUTH", "#b00020", "bad token (check Options)");
  } else if (resp.status === 413) {
    await setBadge(tabId, "BIG", "#b00020", "page too large for the server");
  } else if (resp.ok && data.ok) {
    if (data.new) {
      await setBadge(tabId, "NEW", "#1b7f3b", `saved as job #${data.job_id}`);
    } else {
      await setBadge(tabId, "OK", "#1565c0", `already saved (job #${data.job_id})`);
    }
  } else {
    const why = data.error || `HTTP ${resp.status}`;
    await setBadge(tabId, "ERR", "#b00020", `not saved: ${why}`);
  }
}
