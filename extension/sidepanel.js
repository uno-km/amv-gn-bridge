/**
 * Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0 OR Commercial-License
 */

const DEFAULT_GATEWAY = "http://127.0.0.1:11501/v1";
const DEFAULT_MODEL = "gemini-web";

let gatewayUrl = DEFAULT_GATEWAY;
let selectedModel = DEFAULT_MODEL;
let messageHistory = [];
let isInferring = false;

const statusPill = document.getElementById("statusPill");
const settingsBtn = document.getElementById("settingsBtn");
const settingsDrawer = document.getElementById("settingsDrawer");
const gatewayUrlInput = document.getElementById("gatewayUrlInput");
const modelSelect = document.getElementById("modelSelect");
const saveSettingsBtn = document.getElementById("saveSettingsBtn");
const testGatewayBtn = document.getElementById("testGatewayBtn");
const clearBtn = document.getElementById("clearBtn");
const chatViewport = document.getElementById("chatViewport");
const welcomeCard = document.getElementById("welcomeCard");
const chatForm = document.getElementById("chatForm");
const promptInput = document.getElementById("promptInput");
const sendBtn = document.getElementById("sendBtn");
const currentModelLabel = document.getElementById("currentModelLabel");
const poolBadge = document.getElementById("poolBadge");

async function initSettings() {
  try {
    const data = await chrome.storage.local.get(["gnGatewayUrl", "gnSelectedModel", "pendingPrompt"]);
    if (data.gnGatewayUrl) gatewayUrl = data.gnGatewayUrl;
    if (data.gnSelectedModel) selectedModel = data.gnSelectedModel;
    gatewayUrlInput.value = gatewayUrl;
    modelSelect.value = selectedModel;
    currentModelLabel.textContent = selectedModel;

    if (data.pendingPrompt && data.pendingPrompt.text && Date.now() - data.pendingPrompt.timestamp < 30000) {
      await chrome.storage.local.remove("pendingPrompt");
      sendPrompt(data.pendingPrompt.text);
    }
  } catch (e) {
    console.warn("Storage fallback:", e);
  }
  checkGatewayHealth();
}

function updateStatus(state) {
  const dot = statusPill.querySelector(".status-dot");
  if (dot) dot.className = `status-dot status-${state}`;
}

async function checkGatewayHealth() {
  try {
    const rootUrl = gatewayUrl.replace(/\/v1\/?$/, "");
    const res = await fetch(`${rootUrl}/health`, { method: "GET", signal: AbortSignal.timeout(2500) });
    if (res.ok) {
      const data = await res.json();
      updateStatus("idle");
      if (poolBadge && typeof data.accounts_available === "number") {
        poolBadge.textContent = `가용 ${data.accounts_available}/${data.accounts_total}`;
      }
    } else {
      updateStatus("offline");
    }
  } catch {
    updateStatus("offline");
  }
}

function formatMarkdown(text) {
  if (!text) return "";
  let esc = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  esc = esc.replace(/```([\w-]*)\n([\s\S]*?)```/g, (_, lang, code) => `<pre><code class="language-${lang}">${code.trim()}</code></pre>`);
  esc = esc.replace(/`([^`]+)`/g, "<code>$1</code>");
  esc = esc.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  esc = esc.replace(/\n/g, "<br>");
  return esc;
}

function appendUserMessage(text) {
  if (welcomeCard) welcomeCard.style.display = "none";
  const wrap = document.createElement("div");
  wrap.className = "msg-wrapper user";
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.innerHTML = formatMarkdown(text);
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.innerHTML = `<span>나</span>`;
  wrap.appendChild(bubble);
  wrap.appendChild(meta);
  chatViewport.appendChild(wrap);
  chatViewport.scrollTop = chatViewport.scrollHeight;
}

function appendAssistantPlaceholder() {
  if (welcomeCard) welcomeCard.style.display = "none";
  const wrap = document.createElement("div");
  wrap.className = "msg-wrapper assistant";
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.innerHTML = `<span class="typing-cursor"></span>`;
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.innerHTML = `<span>${selectedModel}</span><button type="button" class="copy-msg-btn">복사</button>`;
  meta.querySelector(".copy-msg-btn").addEventListener("click", () => {
    navigator.clipboard.writeText(wrap.getAttribute("data-raw-content") || "");
    meta.querySelector(".copy-msg-btn").textContent = "복사됨 ✓";
    setTimeout(() => { meta.querySelector(".copy-msg-btn").textContent = "복사"; }, 1500);
  });
  wrap.appendChild(bubble);
  wrap.appendChild(meta);
  chatViewport.appendChild(wrap);
  chatViewport.scrollTop = chatViewport.scrollHeight;
  return { wrap, bubble };
}

async function sendPrompt(promptText) {
  if (!promptText || isInferring) return;
  isInferring = true;
  sendBtn.disabled = true;
  updateStatus("busy");

  appendUserMessage(promptText);
  const { wrap, bubble } = appendAssistantPlaceholder();

  try {
    if (selectedModel === "gemini-web-image") {
      const endpoint = `${gatewayUrl.replace(/\/$/, "")}/images/generations`;
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Bridge-Source": "extension" },
        body: JSON.stringify({ prompt: promptText, model: "gemini-web-image" })
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data?.error?.message || `HTTP ${response.status}`);
      }
      const items = data.data || [];
      if (!items.length) throw new Error("생성된 이미지가 없습니다.");
      const dataUri = `data:${items[0].mime_type || "image/png"};base64,${items[0].b64_json}`;
      bubble.innerHTML = `<a href="${dataUri}" download="gemini_generated.png" title="클릭하여 원본 다운로드"><img src="${dataUri}" style="max-width:100%;border-radius:10px;display:block;"/></a><div style="font-size:11px;opacity:.75;margin-top:6px;">계정: ${data.account_name || ""}:${data.worker_port || ""} (클릭 시 다운로드)</div>`;
      wrap.setAttribute("data-raw-content", dataUri);
    } else {
      messageHistory.push({ role: "user", content: promptText });
      const endpoint = `${gatewayUrl.replace(/\/$/, "")}/chat/completions`;
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Bridge-Source": "extension" },
        body: JSON.stringify({ model: selectedModel, messages: messageHistory, stream: true })
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      let accumulated = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith("data:")) continue;
          const payload = trimmed.replace(/^data:\s*/, "");
          if (payload === "[DONE]") break;
          try {
            const parsed = JSON.parse(payload);
            const delta = parsed?.choices?.[0]?.delta?.content || "";
            if (delta) {
              accumulated += delta;
              bubble.innerHTML = formatMarkdown(accumulated) + `<span class="typing-cursor"></span>`;
              chatViewport.scrollTop = chatViewport.scrollHeight;
            }
          } catch {}
        }
      }
      bubble.innerHTML = formatMarkdown(accumulated);
      wrap.setAttribute("data-raw-content", accumulated);
      messageHistory.push({ role: "assistant", content: accumulated });
    }
  } catch (err) {
    bubble.innerHTML = `<span style="color:#dc2626;">[오류] ${err.message}</span>`;
    wrap.setAttribute("data-raw-content", err.message);
  } finally {
    isInferring = false;
    sendBtn.disabled = false;
    updateStatus("idle");
    checkGatewayHealth();
    chatViewport.scrollTop = chatViewport.scrollHeight;
    promptInput.focus();
  }
}

chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = promptInput.value.trim();
  if (text) {
    promptInput.value = "";
    promptInput.style.height = "auto";
    sendPrompt(text);
  }
});

promptInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    chatForm.dispatchEvent(new Event("submit"));
  }
});

settingsBtn.addEventListener("click", () => {
  settingsDrawer.style.display = settingsDrawer.style.display === "none" ? "block" : "none";
});

saveSettingsBtn.addEventListener("click", async () => {
  gatewayUrl = gatewayUrlInput.value.trim() || DEFAULT_GATEWAY;
  selectedModel = modelSelect.value || DEFAULT_MODEL;
  currentModelLabel.textContent = selectedModel;
  await chrome.storage.local.set({ gnGatewayUrl: gatewayUrl, gnSelectedModel: selectedModel });
  settingsDrawer.style.display = "none";
  checkGatewayHealth();
});

testGatewayBtn.addEventListener("click", () => checkGatewayHealth());

clearBtn.addEventListener("click", () => {
  messageHistory = [];
  chatViewport.innerHTML = "";
  if (welcomeCard) {
    welcomeCard.style.display = "block";
    chatViewport.appendChild(welcomeCard);
  }
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "TRIGGER_PROMPT" && msg.prompt) {
    sendPrompt(msg.prompt);
  }
});

initSettings();
