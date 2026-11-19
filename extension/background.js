/**
 * Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0 OR Commercial-License
 */

if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "gemini-explain-selection",
    title: "Gemini로 선택 텍스트 심층 분석하기",
    contexts: ["selection"]
  });

  chrome.contextMenus.create({
    id: "gemini-summarize-page",
    title: "Gemini로 이 웹페이지 요약하기",
    contexts: ["page"]
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (!tab || !tab.id) return;

  if (chrome.sidePanel && chrome.sidePanel.open) {
    try {
      await chrome.sidePanel.open({ tabId: tab.id });
    } catch (e) {
      console.warn("Failed to open side panel:", e);
    }
  }

  let prompt = "";
  if (info.menuItemId === "gemini-explain-selection" && info.selectionText) {
    prompt = `다음 텍스트의 핵심을 요약하고 심층 분석해줘:\n\n"${info.selectionText}"`;
  } else if (info.menuItemId === "gemini-summarize-page" && tab.url) {
    prompt = `현재 페이지(${tab.title || tab.url})의 핵심 내용을 개요, 주요 포인트, 결론으로 정리해줘.`;
  }

  if (prompt) {
    await chrome.storage.local.set({
      pendingPrompt: { text: prompt, timestamp: Date.now() }
    });
    chrome.runtime.sendMessage({ type: "TRIGGER_PROMPT", prompt }).catch(() => {});
  }
});
