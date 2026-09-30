import { DEFAULTS, getSettings, originPattern, saveSettings } from "./common.js";

const $ = (id) => document.getElementById(id);

function show(msg, ok = true) {
  $("status").textContent = msg;
  $("status").style.color = ok ? "#1b7f3b" : "#b00020";
}

async function load() {
  const s = await getSettings();
  $("serverUrl").value = s.serverUrl;
  $("token").value = s.token;
}

$("save").addEventListener("click", async () => {
  const serverUrl = ($("serverUrl").value || DEFAULTS.serverUrl).trim();
  const token = $("token").value.trim();
  let pattern;
  try {
    const u = new URL(serverUrl);
    if (!/^https?:$/.test(u.protocol)) throw new Error("http(s) only");
    pattern = originPattern(serverUrl);
  } catch (e) {
    show(`Invalid URL: ${e.message}`, false);
    return;
  }
  // Ask for access to exactly this server's host (must run inside the click handler).
  const granted = await chrome.permissions.request({ origins: [pattern] });
  if (!granted) {
    show(`Permission for ${pattern} was not granted`, false);
    return;
  }
  await saveSettings({ serverUrl, token });
  show("Saved");
});

load();
