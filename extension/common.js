// Shared settings helpers for the background worker and the options page.

export const DEFAULTS = {
  serverUrl: "http://127.0.0.1:8765/api/capture",
  token: "",
};

// Token and server URL stay in local storage (not synced to the Google account).
export async function getSettings() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...stored };
}

export async function saveSettings(settings) {
  await chrome.storage.local.set(settings);
}

// Match pattern covering the configured server's origin, e.g. "http://192.168.1.20/*".
// Chrome match patterns without a port match any port on that host.
export function originPattern(serverUrl) {
  const u = new URL(serverUrl);
  return `${u.protocol}//${u.hostname}/*`;
}
