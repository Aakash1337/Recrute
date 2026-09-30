// "Save to Recrute" service worker: toolbar button + context menu -> POST the current page's
// {url, title, html} to the Recrute server. Only runs on an explicit user gesture (activeTab).

import { getSettings, originPattern } from "./common.js";

const MENU_ID = "save-to-recrute";
const MAX_HTML_CHARS = 8 * 1024 * 1024; // keep requests bounded on huge pages

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
  const [result] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => ({
      url: location.href,
      title: document.title,
      html: document.documentElement.outerHTML,
    }),
  });
  return result && result.result;
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
  if (page.html.length > MAX_HTML_CHARS) page.html = page.html.slice(0, MAX_HTML_CHARS);

  let resp;
  try {
    resp = await fetch(settings.serverUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Recrute-Token": settings.token,
      },
      body: JSON.stringify({ url: page.url, title: page.title, html: page.html }),
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
