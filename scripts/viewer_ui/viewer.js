(function () {
  "use strict";

  const SESSION_ID = "viewer_" + Math.random().toString(36).slice(2) + "_" + Date.now();
  const I18N = {
    ko: {
      title: "Workspace 뷰어",
      searchPlaceholder: "파일 또는 주제 경로 검색...",
      expandAll: "모두 펼치기",
      collapseAll: "모두 접기",
      selectPrompt: "좌측 트리에서 문서를 선택하세요.",
      emptyTree: "표시할 마크다운 파일이 없습니다.",
      statsReviews: "리뷰",
      statsSubjects: "주제 문서",
      statsLogs: "원본 일지",
      loadError: "문서를 불러오지 못했습니다: ",
    },
    en: {
      title: "Workspace Viewer",
      searchPlaceholder: "Filter files or topic paths...",
      expandAll: "Expand all",
      collapseAll: "Collapse all",
      selectPrompt: "Select a document from the left folder tree.",
      emptyTree: "No matching Markdown files found.",
      statsReviews: "Reviews",
      statsSubjects: "Subjects",
      statsLogs: "Daily Logs",
      loadError: "Could not load document: ",
    },
  };

  let state = {
    lang: "ko",
    sections: [],
    stats: {},
    currentPath: null,
    currentContent: "",
    viewMode: "rendered",
    searchQuery: "",
    collapsedPaths: new Set(),
    history: [],
    historyIndex: -1,
  };

  const els = {
    appTitle: document.getElementById("app-title"),
    langToggle: document.getElementById("lang-toggle"),
    refreshTree: document.getElementById("refresh-tree"),
    statsBar: document.getElementById("stats-bar"),
    treeSearch: document.getElementById("tree-search"),
    expandAll: document.getElementById("expand-all"),
    collapseAll: document.getElementById("collapse-all"),
    folderTree: document.getElementById("folder-tree"),
    historyBack: document.getElementById("history-back"),
    historyForward: document.getElementById("history-forward"),
    breadcrumb: document.getElementById("breadcrumb"),
    modeRendered: document.getElementById("mode-rendered"),
    modeRaw: document.getElementById("mode-raw"),
    markdownView: document.getElementById("markdown-view"),
    rawView: document.getElementById("raw-view"),
    readerScroll: document.getElementById("reader-scroll"),
  };

  function t(key) {
    return (I18N[state.lang] || I18N.ko)[key] || key;
  }

  function applyLanguageUI() {
    document.documentElement.lang = state.lang;
    els.appTitle.textContent = t("title");
    els.langToggle.textContent = state.lang === "ko" ? "EN" : "한글";
    els.treeSearch.placeholder = t("searchPlaceholder");
    els.expandAll.textContent = t("expandAll");
    els.collapseAll.textContent = t("collapseAll");
    if (!state.currentPath) {
      els.breadcrumb.textContent = t("selectPrompt");
    }
    renderStats();
    renderTree();
  }

  function renderStats() {
    els.statsBar.innerHTML = "";
    const counts = {};
    for (const sec of state.sections) {
      counts[sec.id] = sec.count || 0;
    }
    const pills = [
      `${t("statsReviews")} ${counts.Topic_Reviews || 0}`,
      `${t("statsSubjects")} ${counts.Subject || 0}`,
      `${t("statsLogs")} ${counts.Daily_Logs || 0}`,
    ];
    for (const text of pills) {
      const span = document.createElement("span");
      span.className = "stat-pill";
      span.textContent = text;
      els.statsBar.appendChild(span);
    }
  }

  function filterNodes(nodes, query) {
    if (!query) return nodes;
    const q = query.toLowerCase();
    const out = [];
    for (const node of nodes) {
      if (node.type === "file") {
        if (node.name.toLowerCase().includes(q) || node.path.toLowerCase().includes(q)) {
          out.push(node);
        }
      } else if (node.type === "folder") {
        const filteredChildren = filterNodes(node.children || [], query);
        if (filteredChildren.length > 0 || node.name.toLowerCase().includes(q)) {
          out.push(Object.assign({}, node, { children: filteredChildren }));
        }
      }
    }
    return out;
  }

  function renderNodeList(nodes, container) {
    for (const node of nodes) {
      if (node.type === "folder") {
        const isCollapsed = !state.searchQuery && state.collapsedPaths.has(node.path);
        const row = document.createElement("div");
        row.className = "tree-folder-row";
        row.innerHTML = `<span class="tree-icon">${isCollapsed ? "▸" : "▾"}</span><span class="tree-label"></span>`;
        row.querySelector(".tree-label").textContent = `${node.name} (${node.count})`;
        row.addEventListener("click", () => {
          if (state.collapsedPaths.has(node.path)) {
            state.collapsedPaths.delete(node.path);
          } else {
            state.collapsedPaths.add(node.path);
          }
          renderTree();
        });
        container.appendChild(row);

        if (!isCollapsed && node.children && node.children.length > 0) {
          const childWrap = document.createElement("div");
          childWrap.className = "tree-children";
          renderNodeList(node.children, childWrap);
          container.appendChild(childWrap);
        }
      } else if (node.type === "file") {
        const row = document.createElement("div");
        row.className = "tree-file-row" + (state.currentPath === node.path ? " active" : "");
        row.innerHTML = `<span class="tree-icon">📄</span><span class="tree-label"></span>`;
        row.querySelector(".tree-label").textContent = node.name.replace(/\.md$/i, "");
        row.title = node.path;
        row.addEventListener("click", () => openFile(node.path, true));
        container.appendChild(row);
      }
    }
  }

  function renderTree() {
    els.folderTree.innerHTML = "";
    let totalRendered = 0;

    for (const sec of state.sections) {
      const filteredChildren = filterNodes(sec.children || [], state.searchQuery);
      if (state.searchQuery && filteredChildren.length === 0) continue;

      const secDiv = document.createElement("div");
      secDiv.className = "tree-section";

      const isCollapsed = !state.searchQuery && state.collapsedPaths.has("sec:" + sec.id);
      const header = document.createElement("div");
      header.className = "tree-section-header";
      const label = state.lang === "ko" ? sec.label_ko : sec.label_en;
      header.innerHTML = `<span>${isCollapsed ? "▸" : "▾"} ${escapeHtml(label)}</span><span class="tree-count">${sec.count}</span>`;
      header.addEventListener("click", () => {
        const key = "sec:" + sec.id;
        if (state.collapsedPaths.has(key)) {
          state.collapsedPaths.delete(key);
        } else {
          state.collapsedPaths.add(key);
        }
        renderTree();
      });
      secDiv.appendChild(header);

      if (!isCollapsed && filteredChildren.length > 0) {
        const childrenContainer = document.createElement("div");
        childrenContainer.className = "tree-children";
        renderNodeList(filteredChildren, childrenContainer);
        secDiv.appendChild(childrenContainer);
      }
      els.folderTree.appendChild(secDiv);
      totalRendered += filteredChildren.length;
    }

    if (totalRendered === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-notice";
      empty.textContent = t("emptyTree");
      els.folderTree.appendChild(empty);
    }
  }

  function collectAllFolderPaths(nodes, out) {
    for (const n of nodes) {
      if (n.type === "folder") {
        out.push(n.path);
        collectAllFolderPaths(n.children || [], out);
      }
    }
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function normalizePosixPath(pathStr) {
    const parts = String(pathStr || "").replace(/\\/g, "/").split("/");
    const stack = [];
    for (const part of parts) {
      if (!part || part === ".") continue;
      if (part === "..") {
        stack.pop();
      } else {
        stack.push(part);
      }
    }
    return stack.join("/");
  }

  function resolveRelativeTarget(currentFilePath, rawTarget) {
    let target = String(rawTarget || "").trim();
    if (target.startsWith("<") && target.endsWith(">")) {
      target = target.slice(1, -1).trim();
    }
    const spaceQuote = target.match(/\s+["'(]/);
    if (spaceQuote) {
      target = target.slice(0, spaceQuote.index).trim();
    }
    const hashIdx = target.indexOf("#");
    const hash = hashIdx >= 0 ? target.slice(hashIdx) : "";
    const pathPart = hashIdx >= 0 ? target.slice(0, hashIdx) : target;

    if (!pathPart || /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(pathPart)) {
      return { external: true, url: target };
    }

    let decoded = pathPart;
    try {
      decoded = decodeURIComponent(pathPart);
    } catch (_e) {}

    const baseDir = currentFilePath.includes("/")
      ? currentFilePath.slice(0, currentFilePath.lastIndexOf("/"))
      : "";
    const joined = decoded.startsWith("/")
      ? normalizePosixPath(decoded.slice(1))
      : normalizePosixPath((baseDir ? baseDir + "/" : "") + decoded);

    return {
      external: false,
      path: joined,
      hash: hash,
      isDirectory: decoded.endsWith("/"),
      isMarkdown: /\.md$/i.test(joined),
    };
  }

  function renderInline(text, currentFilePath) {
    const codeTokens = [];
    let working = String(text || "").replace(/`([^`]+)`/g, (_m, code) => {
      const id = `\x00CODE${codeTokens.length}\x00`;
      codeTokens.push(`<code>${escapeHtml(code)}</code>`);
      return id;
    });

    working = escapeHtml(working);

    // Obsidian wikilink images: ![[path|alt]]
    working = working.replace(/!\[\[([^\]]+)\]\]/g, (_m, inner) => {
      const parts = inner.split("|");
      const rawPath = parts[0].trim();
      const alt = (parts[1] || rawPath).trim();
      const target = rawPath.includes("/") ? rawPath : "attachments/" + rawPath;
      const resolved = resolveRelativeTarget(currentFilePath, target);
      const src = resolved.external ? resolved.url : "/workspace/" + encodeURI(resolved.path);
      return `<img src="${escapeHtml(src)}" alt="${escapeHtml(alt)}">`;
    });

    // Standard Markdown images: ![alt](url)
    working = working.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_m, alt, rawUrl) => {
      const resolved = resolveRelativeTarget(currentFilePath, rawUrl);
      const src = resolved.external ? resolved.url : "/workspace/" + encodeURI(resolved.path);
      return `<img src="${escapeHtml(src)}" alt="${escapeHtml(alt)}">`;
    });

    // Standard Markdown links: [label](url)
    working = working.replace(/\[((?:\\.|[^\]])*)\]\(([^)]+)\)/g, (_m, label, rawUrl) => {
      const cleanLabel = label.replace(/\\([\[\]\\])/g, "$1");
      const resolved = resolveRelativeTarget(currentFilePath, rawUrl);
      if (resolved.external) {
        return `<a href="${escapeHtml(resolved.url)}" target="_blank" rel="noopener noreferrer">${cleanLabel}</a>`;
      }
      if (resolved.isMarkdown) {
        return `<a href="#" data-md-path="${escapeHtml(resolved.path)}">${cleanLabel}</a>`;
      }
      if (resolved.isDirectory) {
        return `<a href="#" data-folder-path="${escapeHtml(resolved.path)}">${cleanLabel}</a>`;
      }
      return `<a href="/workspace/${encodeURI(resolved.path)}" target="_blank">${cleanLabel}</a>`;
    });

    working = working.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    working = working.replace(/\*([^*]+)\*/g, "<em>$1</em>");

    working = working.replace(/\x00CODE(\d+)\x00/g, (_m, idx) => codeTokens[Number(idx)] || "");
    return working;
  }

  function splitTableCells(line) {
    let trimmed = line.trim();
    if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
    if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
    return trimmed.split("|").map((c) => c.trim());
  }

  function isTableDividerLine(line) {
    const tLine = line.trim();
    if (!tLine.startsWith("|")) return false;
    const cells = splitTableCells(tLine);
    return cells.length > 0 && cells.every((c) => /^:?-{3,}:?$/.test(c));
  }

  function renderMarkdownToHtml(markdown, currentFilePath) {
    const cleaned = String(markdown || "").replace(/<!--[\s\S]*?-->/g, "");
    const lines = cleaned.replace(/\r\n/g, "\n").split("\n");
    const html = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];
      const trimmed = line.trim();

      if (!trimmed) {
        i++;
        continue;
      }

      // Fenced code block
      const fenceMatch = trimmed.match(/^(```+|~~~+)(.*)$/);
      if (fenceMatch) {
        const fence = fenceMatch[1];
        const codeLines = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) {
          codeLines.push(lines[i]);
          i++;
        }
        if (i < lines.length) i++;
        html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        continue;
      }

      // Heading
      const headingMatch = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/);
      if (headingMatch) {
        const level = headingMatch[1].length;
        const content = renderInline(headingMatch[2].trim(), currentFilePath);
        html.push(`<h${level}>${content}</h${level}>`);
        i++;
        continue;
      }

      // Horizontal rule
      if (/^(\*{3,}|-{3,}|_{3,})$/.test(trimmed)) {
        html.push("<hr>");
        i++;
        continue;
      }

      // Table
      if (trimmed.startsWith("|") && i + 1 < lines.length && isTableDividerLine(lines[i + 1])) {
        const headers = splitTableCells(lines[i]);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim().startsWith("|")) {
          rows.push(splitTableCells(lines[i]));
          i++;
        }
        let tableHtml = "<table><thead><tr>";
        for (const h of headers) {
          tableHtml += `<th>${renderInline(h, currentFilePath)}</th>`;
        }
        tableHtml += "</tr></thead><tbody>";
        for (const r of rows) {
          tableHtml += "<tr>";
          for (let c = 0; c < headers.length; c++) {
            tableHtml += `<td>${renderInline(r[c] || "", currentFilePath)}</td>`;
          }
          tableHtml += "</tr>";
        }
        tableHtml += "</tbody></table>";
        html.push(tableHtml);
        continue;
      }

      // Blockquote
      if (trimmed.startsWith(">")) {
        const quoteLines = [];
        while (i < lines.length && lines[i].trim().startsWith(">")) {
          quoteLines.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        html.push(`<blockquote>${renderMarkdownToHtml(quoteLines.join("\n"), currentFilePath)}</blockquote>`);
        continue;
      }

      // Lists
      const ulMatch = line.match(/^\s*[-*+]\s+(.*)$/);
      const olMatch = line.match(/^\s*\d+\.\s+(.*)$/);
      if (ulMatch || olMatch) {
        const isOrdered = Boolean(olMatch);
        const tag = isOrdered ? "ol" : "ul";
        const items = [];
        while (i < lines.length) {
          const m = isOrdered
            ? lines[i].match(/^\s*\d+\.\s+(.*)$/)
            : lines[i].match(/^\s*[-*+]\s+(.*)$/);
          if (!m) break;
          items.push(`<li>${renderInline(m[1], currentFilePath)}</li>`);
          i++;
        }
        html.push(`<${tag}>${items.join("")}</${tag}>`);
        continue;
      }

      // Paragraph
      const paraLines = [];
      while (
        i < lines.length &&
        lines[i].trim() &&
        !lines[i].trim().startsWith("```") &&
        !lines[i].trim().startsWith("~~~") &&
        !lines[i].trim().startsWith(">") &&
        !/^\s{0,3}#{1,6}\s+/.test(lines[i]) &&
        !/^(\*{3,}|-{3,}|_{3,})$/.test(lines[i].trim()) &&
        !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) &&
        !(lines[i].trim().startsWith("|") && i + 1 < lines.length && isTableDividerLine(lines[i + 1]))
      ) {
        paraLines.push(renderInline(lines[i], currentFilePath));
        i++;
      }
      html.push(`<p>${paraLines.join("<br>")}</p>`);
    }

    return html.join("\n");
  }

  function updateHistoryButtons() {
    els.historyBack.disabled = state.historyIndex <= 0;
    els.historyForward.disabled = state.historyIndex >= state.history.length - 1;
  }

  function setViewMode(mode) {
    state.viewMode = mode;
    els.modeRendered.classList.toggle("active", mode === "rendered");
    els.modeRaw.classList.toggle("active", mode === "raw");
    els.markdownView.hidden = mode !== "rendered";
    els.rawView.hidden = mode !== "raw";
  }

  async function openFile(relPath, pushHistory) {
    try {
      const res = await fetch("/api/file?path=" + encodeURIComponent(relPath));
      const data = await res.json();
      if (!res.ok || data.status !== "ok") {
        throw new Error(data.message || "HTTP " + res.status);
      }
      state.currentPath = data.path;
      state.currentContent = data.content;
      els.breadcrumb.textContent = data.path;
      els.rawView.textContent = data.content;
      els.markdownView.innerHTML = renderMarkdownToHtml(data.content, data.path);
      els.readerScroll.scrollTop = 0;

      if (pushHistory) {
        state.history = state.history.slice(0, state.historyIndex + 1);
        if (state.history[state.history.length - 1] !== data.path) {
          state.history.push(data.path);
          state.historyIndex = state.history.length - 1;
        }
      }
      updateHistoryButtons();
      renderTree();
    } catch (err) {
      els.markdownView.innerHTML = `<div class="empty-notice">${escapeHtml(t("loadError") + err.message)}</div>`;
    }
  }

  async function loadTree(selectDefault) {
    const res = await fetch("/api/tree");
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      if (selectDefault && data.language) {
        state.lang = data.language === "en" ? "en" : "ko";
      }
      state.sections = data.sections || [];
      state.stats = data.stats || {};
      applyLanguageUI();
      if (selectDefault && !state.currentPath && data.default_file) {
        await openFile(data.default_file, true);
      }
    }
  }

  function setupSessionLifecycle() {
    fetch("/api/session/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: SESSION_ID }),
    }).catch(() => {});

    try {
      const es = new EventSource("/api/session/watch?session_id=" + encodeURIComponent(SESSION_ID));
      window.addEventListener("beforeunload", () => es.close());
    } catch (_e) {}

    window.addEventListener("pagehide", () => {
      const payload = JSON.stringify({ session_id: SESSION_ID });
      if (navigator.sendBeacon) {
        const blob = new Blob([payload], { type: "application/json" });
        navigator.sendBeacon("/api/session/close", blob);
      }
    });
  }

  // Event bindings
  els.langToggle.addEventListener("click", () => {
    state.lang = state.lang === "ko" ? "en" : "ko";
    applyLanguageUI();
  });

  els.refreshTree.addEventListener("click", () => loadTree(false));

  els.treeSearch.addEventListener("input", (e) => {
    state.searchQuery = e.target.value.trim();
    renderTree();
  });

  els.expandAll.addEventListener("click", () => {
    state.collapsedPaths.clear();
    renderTree();
  });

  els.collapseAll.addEventListener("click", () => {
    for (const sec of state.sections) {
      const paths = [];
      collectAllFolderPaths(sec.children || [], paths);
      for (const p of paths) state.collapsedPaths.add(p);
    }
    renderTree();
  });

  els.historyBack.addEventListener("click", () => {
    if (state.historyIndex > 0) {
      state.historyIndex--;
      openFile(state.history[state.historyIndex], false);
    }
  });

  els.historyForward.addEventListener("click", () => {
    if (state.historyIndex < state.history.length - 1) {
      state.historyIndex++;
      openFile(state.history[state.historyIndex], false);
    }
  });

  els.modeRendered.addEventListener("click", () => setViewMode("rendered"));
  els.modeRaw.addEventListener("click", () => setViewMode("raw"));

  els.markdownView.addEventListener("click", (e) => {
    const link = e.target.closest("a");
    if (!link) return;
    const mdPath = link.getAttribute("data-md-path");
    if (mdPath) {
      e.preventDefault();
      openFile(mdPath, true);
      return;
    }
    const folderPath = link.getAttribute("data-folder-path");
    if (folderPath) {
      e.preventDefault();
      state.searchQuery = folderPath;
      els.treeSearch.value = folderPath;
      renderTree();
    }
  });

  setupSessionLifecycle();
  loadTree(true);
})();
