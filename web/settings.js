import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Kotodama's connection settings, editable from ComfyUI's Settings dialog.
//
// Nothing here is stored in ComfyUI's own settings file: that file can be read
// back through ComfyUI's API, and it would expose the API key. The form saves to
// Kotodama's route, which writes ComfyUI's user directory. The key is write-only:
// the server never sends it back, so this form only ever shows "set" or "not set".

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

const INPUT_STYLE = "width:100%;box-sizing:border-box;padding:6px 8px;border-radius:6px;" +
  "background:var(--comfy-input-bg);color:var(--input-text);border:1px solid var(--border-color)";
const BUTTON_STYLE = "padding:6px 12px;border-radius:6px;cursor:pointer;" +
  "background:var(--comfy-input-bg);color:var(--input-text);border:1px solid var(--border-color)";

function field(label, input, note) {
  const wrap = el("label", { style: "display:block;margin:0.5em 0" },
    el("div", { textContent: label, style: "font-weight:600" }), input);
  if (note) wrap.append(el("div", { textContent: note, style: "opacity:0.7;font-size:0.85em" }));
  return wrap;
}

const ERRORS = {
  cross_origin: "Refused: settings can only be saved from this ComfyUI page.",
  confirm_url_change: "Changing the endpoint needs confirmation.",
  key_outside_panel: "Your API key is still in the node folder's legacy .env, which this panel cannot remove. " +
    "Delete it there (or move it here by saving a new key after removing it), then try again.",
  invalid_url: "The endpoint must be an http(s) URL without a username, password, query or fragment.",
  invalid_api_key: "The API key cannot be blank. Use Clear key to remove it.",
  invalid_timeout: "Timeout must be a number of seconds from 1 to 3600.",
  invalid_fallback_models: "Fallback models must be a comma-separated list of model IDs.",
  no_user_directory: "ComfyUI's user directory is unavailable, so settings cannot be saved.",
};

function settingsPanel() {
  const row = el("tr");
  const cell = el("td", { colSpan: 2 });

  const status = el("div", { textContent: "Loading…" });
  const url = el("input", { type: "url", placeholder: "http://127.0.0.1:4000", style: INPUT_STYLE });
  const key = el("input", { type: "password", autocomplete: "new-password", placeholder: "", style: INPUT_STYLE });
  const clearKey = el("button", { type: "button", textContent: "Clear key", style: BUTTON_STYLE });
  const models = el("input", { type: "text", placeholder: "model-id, other-model", style: INPUT_STYLE });
  const timeout = el("input", { type: "number", min: 1, max: 3600, step: 1, style: INPUT_STYLE + ";width:8em" });
  const save = el("button", { type: "button", textContent: "Save", style: BUTTON_STYLE });
  const test = el("button", { type: "button", textContent: "Test connection", style: BUTTON_STYLE });
  const message = el("div", { style: "margin-top:0.5em" });
  let saved = { url: "" };

  function show(data) {
    saved = data;
    url.value = data.url || "";
    key.value = "";
    key.placeholder = data.key_set ? "•••••••• (set; type a new key to replace it)" : "not set";
    models.value = (data.fallback_models || []).join(", ");
    timeout.value = Math.round(data.timeout || 300);
    const legacy = Object.values(data.source || {}).includes("dotenv");
    status.textContent = legacy
      ? `Some values still come from the node folder's legacy .env. Save them here to move them to ${data.config_location}.`
      : `Saved in ${data.config_location}.`;
    save.disabled = data.writable === false;
  }

  async function refresh() {
    try {
      const response = await api.fetchApi("/kotodama/status");
      if (!response.ok) throw new Error("status unavailable");
      show(await response.json());
    } catch {
      status.textContent = "Kotodama status unavailable; check the server log.";
    }
  }

  async function post(body) {
    const response = await api.fetchApi("/kotodama/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(ERRORS[data.error] || `Save failed (${data.error || response.status}).`);
    return data;
  }

  save.addEventListener("click", async () => {
    const body = {
      fallback_models: models.value,
      timeout: Number(timeout.value) || 300,
    };
    const newUrl = url.value.trim().replace(/\/+$/, "");
    if (newUrl !== (saved.url || "")) {
      const ok = window.confirm(
        `Change Kotodama's endpoint to "${newUrl || "(none)"}"?\n\n` +
        "The saved API key will be cleared unless you entered a new key, " +
        "so your key is never sent to a server you did not choose.");
      if (!ok) return;
      body.base_url = newUrl;
      body.confirm_url_change = true;
    }
    if (key.value.trim()) body.api_key = key.value.trim();
    save.disabled = true;
    message.textContent = "Saving…";
    try {
      show(await post(body));
      message.textContent = "Saved.";
    } catch (err) {
      message.textContent = err.message;
    } finally {
      key.value = "";
      save.disabled = saved.writable === false;
    }
  });

  clearKey.addEventListener("click", async () => {
    if (!window.confirm("Remove the saved Kotodama API key?")) return;
    try {
      show(await post({ clear_api_key: true }));
      message.textContent = "Key cleared.";
    } catch (err) {
      message.textContent = err.message;
    }
  });

  test.addEventListener("click", async () => {
    test.disabled = true;
    message.textContent = "Testing saved settings…";
    try {
      const response = await api.fetchApi("/kotodama/test", { method: "POST" });
      const data = await response.json();
      message.textContent = response.status === 429
        ? "Please wait five seconds before testing again."
        : data.ok ? `Connected (HTTP ${data.status}).`
          : `Connection failed: ${data.error || "unknown"}${data.status ? ` (HTTP ${data.status})` : ""}.`;
    } catch {
      message.textContent = "Connection test unavailable; check the server log.";
    } finally {
      test.disabled = false;
    }
  });

  cell.append(
    status,
    field("Endpoint URL", url, "Root URL of an OpenAI-compatible server, without /v1."),
    field("API key", key, "Write-only: it is saved on the server and never shown again."),
    clearKey,
    field("Fallback models", models, "Offered in the model menu when the server cannot list its models."),
    field("Timeout (seconds)", timeout),
    el("div", { style: "display:flex;gap:0.5em;margin-top:0.5em" }, save, test),
    message,
    el("div", {
      textContent: "Anyone who can use this ComfyUI page can change these settings. Keep ComfyUI private.",
      style: "opacity:0.7;font-size:0.85em;margin-top:0.5em",
    }),
  );
  row.append(cell);
  void refresh();
  return row;
}

app.registerExtension({
  name: "Kotodama.ConnectionSettings",
  setup() {
    app.ui.settings.addSetting({
      id: "Kotodama.Connection",
      name: "Kotodama connection",
      type: settingsPanel,
      defaultValue: null,
    });
  },
});
