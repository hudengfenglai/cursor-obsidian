const { Plugin, Notice, PluginSettingTab, Setting, addIcon, MarkdownView, TFile, ItemView, WorkspaceLeaf } = require("obsidian");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const http = require("http");

const ICON_ID = "cursor-sidecar";
const VIEW_TYPE_EMBEDDED_PANE = "cursor-sidecar-pane";
const ICON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="m18 16 4-4-4-4"/>
  <path d="m6 8-4 4 4 4"/>
  <path d="m14.5 4-5 16"/>
</svg>`;

const DEFAULT_SETTINGS = {
  sidecarDir: "",
  pythonPath: "",
  developerMode: false,
  dataDirOverride: "",
  daemonHost: "127.0.0.1",
  daemonPort: 27845,
  liveSidecar: true,
  contextFollow: false,
  openVaultInCursor: true,
  /** Fixed Cursor project folder. Empty → current Obsidian vault; filled on first load. */
  cursorProjectFolder: "",
  showNotices: true,
  embeddedPaneExperimental: true,
  borderlessCursorExperimental: false,
  embedBackend: "native_child", // "visual" | "native_child"
  cursorExePath: "", // Advanced: optional Cursor.exe override
};

const CONTEXT_FOLLOW_DEBOUNCE_MS = 150;
const CONTEXT_FOLLOW_SUPPRESS_MS = 400;
const PANE_RECT_THROTTLE_MS = 32;

/** Rebase local seq against daemon latest_seq + clock (plugin reload safe). */
function nextContextSyncSeq(localSeq, daemonSeq, nowMs) {
  const local = Number(localSeq) || 0;
  const daemon = Number(daemonSeq) || 0;
  const ms = nowMs == null ? Date.now() : Number(nowMs) || 0;
  const clockBase = ms * 1000;
  return Math.max(local, daemon, clockBase) + 1;
}

class CursorSidecarPaneView extends ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
    this._embeddedUi = false;
    this._hostEl = null;
    this._statusEl = null;
    this._actionsEl = null;
    this._ro = null;
  }

  getViewType() {
    return VIEW_TYPE_EMBEDDED_PANE;
  }

  getDisplayText() {
    return "Cursor Agents";
  }

  getIcon() {
    return ICON_ID;
  }

  async onOpen() {
    const root = this.contentEl;
    root.empty();
    root.addClass("cursor-sidecar-pane");
    root.style.display = "flex";
    root.style.flexDirection = "column";
    root.style.alignItems = "stretch";
    root.style.justifyContent = "center";
    root.style.height = "100%";
    root.style.padding = "12px";
    root.style.boxSizing = "border-box";

    this._hostEl = root.createDiv({ cls: "cursor-sidecar-pane-host" });
    this._hostEl.style.flex = "1";
    this._hostEl.style.minHeight = "0";
    this._hostEl.style.display = "flex";
    this._hostEl.style.flexDirection = "column";
    this._hostEl.style.alignItems = "center";
    this._hostEl.style.justifyContent = "center";
    this._hostEl.style.gap = "10px";

    this._statusEl = this._hostEl.createEl("div", {
      cls: "cursor-sidecar-pane-status",
      text: "Cursor Agents\nNo Agents Window bound",
    });
    this._statusEl.style.whiteSpace = "pre-line";
    this._statusEl.style.textAlign = "center";
    this._statusEl.style.opacity = "0.85";

    this._actionsEl = this._hostEl.createDiv({ cls: "cursor-sidecar-pane-actions" });
    this._actionsEl.style.display = "flex";
    this._actionsEl.style.gap = "8px";
    this._actionsEl.style.flexWrap = "wrap";
    this._actionsEl.style.justifyContent = "center";

    this.renderPaneUi("unbound");
    this.plugin.registerEmbeddedPaneView(this);
    this._installResizeObserver();
    this.plugin.schedulePaneRectUpdate();
    this.plugin.refreshAgentsPaneUi().catch(() => {});
  }

  /**
   * @param {"unbound"|"ready"|"opening"|"embedded"|"anchor"|"failed"|"closed"|"disabled"} mode
   * @param {string} [detail]
   */
  renderPaneUi(mode, detail) {
    this._paneMode = mode || "unbound";
    if (!this._statusEl || !this._actionsEl) return;
    this._actionsEl.empty();
    const root = this.contentEl;
    const host = this._hostEl;
    const setAnchorChrome = (on) => {
      if (root) {
        root.toggleClass("is-native-anchor", !!on);
        root.style.padding = on ? "0" : "12px";
        root.style.justifyContent = on ? "stretch" : "center";
      }
      if (host) {
        host.style.opacity = on ? "0" : "1";
        host.style.pointerEvents = on ? "none" : "";
      }
    };
    setAnchorChrome(false);
    if (!this.plugin.settings.embeddedPaneExperimental) {
      this._statusEl.setText("Cursor Agents\nExperimental setting is OFF");
      this._statusEl.style.display = "";
      const go = this._actionsEl.createEl("button", { text: "Open Settings", cls: "mod-cta" });
      go.onclick = () => {
        this.plugin.app.setting.open();
        this.plugin.app.setting.openTabById("cursor-sidecar");
      };
      return;
    }
    if (mode === "anchor") {
      setAnchorChrome(true);
      this._statusEl.setText("");
      this._statusEl.style.display = "none";
      return;
    }
    this._statusEl.style.display = "";
    if (mode === "opening") {
      this._statusEl.setText("Cursor Agents\nOpening…");
      return;
    }
    if (mode === "failed") {
      this._statusEl.setText(`Cursor Agents\nNative embed failed\n${detail || ""}`);
      const retry = this._actionsEl.createEl("button", { text: "Retry Native", cls: "mod-cta" });
      retry.onclick = async () => {
        await this.plugin.openNativeAgentsPane();
      };
      const exitBtn = this._actionsEl.createEl("button", { text: "Close" });
      exitBtn.onclick = async () => {
        await this.plugin.exitEmbeddedMode({ closePane: true });
      };
      return;
    }
    if (mode === "embedded") {
      this._statusEl.setText("Cursor Agents\nEmbedded (visual)");
      const exitBtn = this._actionsEl.createEl("button", { text: "Exit" });
      exitBtn.onclick = async () => {
        await this.plugin.exitEmbeddedMode({ closePane: false });
      };
      return;
    }
    if (mode === "closed") {
      this._statusEl.setText("Cursor Agents\nAgents Window closed");
      const reopen = this._actionsEl.createEl("button", { text: "Open Native Pane", cls: "mod-cta" });
      reopen.onclick = async () => {
        await this.plugin.openNativeAgentsPane();
      };
      return;
    }
    this._statusEl.setText("Cursor Agents\nReady");
    const open = this._actionsEl.createEl("button", { text: "Open Native Pane", cls: "mod-cta" });
    open.onclick = async () => {
      await this.plugin.openNativeAgentsPane();
    };
  }

  _installResizeObserver() {
    const target = this.contentEl;
    if (!target || typeof ResizeObserver === "undefined") return;
    this._ro = new ResizeObserver(() => {
      this.plugin.schedulePaneRectUpdate();
    });
    this._ro.observe(target);
  }

  getPaneRectPayload() {
    const el = this.contentEl;
    if (!el) {
      return {
        left: 0,
        top: 0,
        width: 0,
        height: 0,
        viewport_width: window.innerWidth || 1,
        viewport_height: window.innerHeight || 1,
        device_pixel_ratio: window.devicePixelRatio || 1,
        visible: false,
      };
    }
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const displayHidden = style.display === "none" || style.visibility === "hidden";
    const nearZero = rect.width < 8 || rect.height < 8;
    const leafVisible = !!(this.leaf && this.leaf.view === this);
    const visible = !displayHidden && !nearZero && leafVisible;
    return {
      left: rect.left,
      top: rect.top,
      width: rect.width,
      height: rect.height,
      viewport_width: window.innerWidth || 1,
      viewport_height: window.innerHeight || 1,
      device_pixel_ratio: window.devicePixelRatio || 1,
      visible,
    };
  }

  async onClose() {
    if (this._ro) {
      try {
        this._ro.disconnect();
      } catch (_e) {
        /* ignore */
      }
      this._ro = null;
    }
    this.plugin.unregisterEmbeddedPaneView(this);
    // Closing the pane exits embed (not Detach)
    if (this.plugin._embedActive) {
      await this.plugin.exitEmbeddedMode({ closePane: false, fromViewClose: true });
    }
  }
}

class CursorSidecarPlugin extends Plugin {
  async onload() {
    const saved = await this.loadData();
    this.settings = Object.assign({}, DEFAULT_SETTINGS, saved || {});
    // Migrate v0.2.1 useDaemon → liveSidecar
    if (saved && saved.liveSidecar === undefined && saved.useDaemon !== undefined) {
      this.settings.liveSidecar = !!saved.useDaemon;
    }
    // Default Cursor project = this Obsidian vault folder (persist once)
    if (!(this.settings.cursorProjectFolder || "").trim()) {
      const vault = this.vaultPath();
      if (vault) {
        this.settings.cursorProjectFolder = vault;
        await this.saveSettings();
      }
    }
    addIcon(ICON_ID, ICON_SVG);

    this._embeddedPaneView = null;
    this._embedActive = false;
    this._nativeVerified = false;
    this._commandBusy = false;
    this._runtimeInfo = null;
    this._paneRaf = null;
    this._paneLastSentAt = 0;
    this._paneThrottleTimer = null;
    this._lastEmbedDiag = null;

    this.registerView(VIEW_TYPE_EMBEDDED_PANE, (leaf) => new CursorSidecarPaneView(leaf, this));

    this.addRibbonIcon(ICON_ID, "Cursor Sidecar: Attach / Detach", async () => {
      await this.runAction("click");
    });

    this.addCommand({
      id: "cursor-sidecar-click",
      name: "Attach / Detach Cursor Sidecar",
      hotkeys: [{ modifiers: ["Mod", "Alt"], key: "C" }],
      callback: async () => this.runAction("click"),
    });
    this.addCommand({
      id: "cursor-sidecar-attach",
      name: "Attach Cursor Sidecar",
      callback: async () => this.runAction("attach"),
    });
    this.addCommand({
      id: "cursor-sidecar-detach",
      name: "Detach Cursor Sidecar",
      callback: async () => this.runAction("detach"),
    });
    this.addCommand({
      id: "cursor-sidecar-arrange",
      name: "Arrange Sidecar",
      callback: async () => this.runAction("arrange"),
    });
    this.addCommand({
      id: "cursor-sidecar-compact",
      name: "Cursor Sidecar: Compact",
      callback: async () => this.runAction("compact"),
    });
    this.addCommand({
      id: "cursor-sidecar-normal",
      name: "Cursor Sidecar: Normal",
      callback: async () => this.runAction("normal"),
    });
    this.addCommand({
      id: "cursor-sidecar-wide",
      name: "Cursor Sidecar: Wide",
      callback: async () => this.runAction("wide"),
    });
    this.addCommand({
      id: "cursor-sidecar-focus",
      name: "Focus Cursor",
      callback: async () => this.runAction("focus"),
    });
    this.addCommand({
      id: "cursor-sidecar-toggle",
      name: "Show / hide Cursor",
      callback: async () => this.runAction("toggle"),
    });
    this.addCommand({
      id: "cursor-sidecar-status",
      name: "Sidecar status",
      callback: async () => this.runAction("status", true),
    });
    this.addCommand({
      id: "cursor-sidecar-open-current-note",
      name: "Cursor Sidecar: Open current note in Cursor",
      callback: async () => this.openCurrentNoteInCursor(),
    });
    this.addCommand({
      id: "cursor-sidecar-open-vault",
      name: "Cursor Sidecar: Open current vault in Cursor",
      callback: async () => this.openVaultInCursor(),
    });
    this.addCommand({
      id: "cursor-sidecar-open-embedded-pane",
      name: "Cursor Sidecar: Open Native Agents Pane",
      callback: async () => this.openNativeAgentsPane(),
    });
    this.addCommand({
      id: "cursor-sidecar-close-embedded-pane",
      name: "Cursor Sidecar: Close Agents Pane",
      callback: async () => this.closeEmbeddedPane(),
    });
    this.addCommand({
      id: "cursor-sidecar-toggle-embedded-mode",
      name: "Cursor Sidecar: Toggle Native Agents Pane",
      callback: async () => this.toggleEmbeddedMode(),
    });
    this.addCommand({
      id: "cursor-sidecar-bind-agents-window",
      name: "Cursor Sidecar: Bind Agents Window (debug)",
      callback: async () => this.bindAgentsWindowFlow(),
    });
    this.addCommand({
      id: "cursor-sidecar-focus-agents-window",
      name: "Cursor Sidecar: Focus Agents Window",
      callback: async () => this.focusAgentsWindow(),
    });
    this.addCommand({
      id: "cursor-sidecar-enter-native-agents-embed",
      name: "Cursor Sidecar: Open Native Agents Pane",
      callback: async () => this.openNativeAgentsPane(),
    });
    this.addCommand({
      id: "cursor-sidecar-exit-native-agents-embed",
      name: "Cursor Sidecar: Exit Native Agents Embed",
      callback: async () => this.exitEmbeddedMode({ closePane: false }),
    });
    this.addCommand({
      id: "cursor-sidecar-recover-native-agents",
      name: "Cursor Sidecar: Recover Native Agents Window",
      callback: async () => this.recoverNativeAgents(),
    });
    this.addCommand({
      id: "cursor-sidecar-temp-hide-agents-child",
      name: "Cursor Sidecar: Temporarily Hide Agents Child",
      callback: async () => this.tempHideAgentsChild(),
    });

    this.registerEvent(
      this.app.workspace.on("file-menu", (menu, file) => {
        if (!(file instanceof TFile)) return;
        menu.addItem((item) => {
          item
            .setTitle("Open in Cursor")
            .setIcon(ICON_ID)
            .onClick(async () => {
              await this.openFileInCursor(file, null, null);
            });
        });
      })
    );

    this.registerEvent(
      this.app.workspace.on("editor-menu", (menu, editor, view) => {
        menu.addItem((item) => {
          item
            .setTitle("Open in Cursor")
            .setIcon(ICON_ID)
            .onClick(async () => {
              const file = view && view.file;
              if (!file) {
                this.notify("No active file", true);
                return;
              }
              const cur = editor.getCursor();
              await this.openFileInCursor(file, cur.line, cur.ch);
            });
        });
      })
    );

    this.registerEvent(
      this.app.workspace.on("layout-change", () => {
        this.schedulePaneRectUpdate();
      })
    );
    this.registerEvent(
      this.app.workspace.on("resize", () => {
        this.schedulePaneRectUpdate();
      })
    );

    this.addSettingTab(new CursorSidecarSettingTab(this.app, this));
    // Fire-and-forget version mismatch notice (packaged only)
    this.checkHelperVersionMismatch().catch(() => {});

    // Context Follow state (plugin-local; no duplicate listeners on reload)
    this._cfTimer = null;
    this._cfGen = 0;
    this._cfSeq = 0;
    this._cfLastPath = null;
    this._cfLastLine = null;
    this._cfLastSyncAt = 0;
    this._cfLastRelative = null;
    this._cfLastErrorAt = 0;
    this._cfLastErrorKey = "";
    this._cfDaemonOk = false;
    this.registerEvent(
      this.app.workspace.on("file-open", (file) => {
        this.onContextFollowFileOpen(file);
      })
    );
  }

  onunload() {
    if (this._cfTimer) {
      clearTimeout(this._cfTimer);
      this._cfTimer = null;
    }
    this._cfGen += 1;
    if (this._paneRaf) {
      cancelAnimationFrame(this._paneRaf);
      this._paneRaf = null;
    }
    if (this._paneThrottleTimer) {
      clearTimeout(this._paneThrottleTimer);
      this._paneThrottleTimer = null;
    }
  }

  async saveSettings() {
    await this.saveData(this.settings);
  }

  notify(message, isError = false) {
    if (!this.settings.showNotices && !isError) return;
    new Notice(message, isError ? 8000 : 4000);
  }

  registerEmbeddedPaneView(view) {
    this._embeddedPaneView = view;
  }

  unregisterEmbeddedPaneView(view) {
    if (this._embeddedPaneView === view) {
      this._embeddedPaneView = null;
    }
  }

  schedulePaneRectUpdate() {
    if (!this._embedActive && !(this._embeddedPaneView && this.settings.embeddedPaneExperimental)) {
      return;
    }
    if (this._paneRaf) return;
    this._paneRaf = requestAnimationFrame(() => {
      this._paneRaf = null;
      const now = Date.now();
      const wait = PANE_RECT_THROTTLE_MS - (now - (this._paneLastSentAt || 0));
      if (wait > 0) {
        if (this._paneThrottleTimer) return;
        this._paneThrottleTimer = setTimeout(() => {
          this._paneThrottleTimer = null;
          this.flushPaneRectUpdate();
        }, wait);
        return;
      }
      this.flushPaneRectUpdate();
    });
  }

  async flushPaneRectUpdate() {
    if (!this._embedActive) return;
    const view = this._embeddedPaneView;
    if (!view) return;
    const pane = view.getPaneRectPayload();
    this._paneLastSentAt = Date.now();
    this._lastEmbedDiag = { dom: pane, at: this._paneLastSentAt };
    try {
      if (!(await this.daemonHealthy())) return;
      const body = { cmd: "update-embedded-pane", pane };
      if (this.settings.borderlessCursorExperimental) {
        body.pane = Object.assign({}, pane, { borderless: true });
      }
      const result = await this.httpJson("POST", "/rpc", body);
      if (result && result.error === "agent_window_closed") {
        this._embedActive = false;
        if (view && view.renderPaneUi) view.renderPaneUi("closed");
        return;
      }
      if (result && result.placement) {
        this._lastEmbedDiag.placement = result.placement;
      }
      if (view && typeof view.renderPaneUi === "function" && this._embedActive) {
        view.renderPaneUi("embedded");
      }
    } catch (_e) {
      /* ignore transient RPC errors during resize */
    }
  }

  async ensureAttachedForEmbed() {
    try {
      const up = await this.ensureDaemon();
      if (!up) return false;
      const st = await this.httpJson("GET", "/status");
      const status = (st && st.status) || st || {};
      if (status.attached) return true;
    } catch (_e) {
      /* fall through */
    }
    return false;
  }

  async refreshAgentsPaneUi() {
    const view = this._embeddedPaneView;
    if (!view || typeof view.renderPaneUi !== "function") return;
    if (this._embedActive) {
      view.renderPaneUi(this._nativeVerified ? "anchor" : "embedded");
      return;
    }
    try {
      if (!(await this.daemonHealthy())) {
        view.renderPaneUi("unbound");
        return;
      }
      const st = await this.httpJson("GET", "/status");
      const status = (st && st.status) || st || {};
      const agent = status.cursor_agent || {};
      const emb = status.embedded || {};
      // Live truth only — never trust stale agent_bound memory alone
      const liveBound = !!(agent.ok || agent.bound || emb.agent_bound);
      if (emb.agent_stale_reason || status.agent_stale_reason) {
        view.renderPaneUi("closed");
        return;
      }
      if (liveBound) {
        view.renderPaneUi("ready");
      } else {
        view.renderPaneUi("unbound");
      }
    } catch (_e) {
      view.renderPaneUi("unbound");
    }
  }

  async bindAgentsWindowFlow() {
    const up = await this.ensureDaemon();
    if (!up) {
      this.notify("Daemon unavailable — cannot bind Agents Window", true);
      return;
    }
    try {
      // Prefer select if a sole non-editor window already exists
      let result = await this.httpJson("POST", "/rpc", {
        cmd: "select-agents-window",
        confirm: false,
      });
      if (result && result.error === "confirm_required" && result.candidate) {
        const ok = window.confirm(
          `Bind this Cursor window as Agents Window?\nHWND ${result.candidate.hwnd}\n${result.candidate.title || ""}`
        );
        if (!ok) return;
        result = await this.httpJson("POST", "/rpc", {
          cmd: "select-agents-window",
          confirm: true,
          hwnd: result.candidate.hwnd,
        });
        if (result && result.ok) {
          this.notify("Agents Window bound");
          await this.refreshAgentsPaneUi();
          return;
        }
      }
      if (result && result.ok) {
        this.notify("Agents Window bound");
        await this.refreshAgentsPaneUi();
        return;
      }

      // Two-step: begin → user opens New Agents Window → complete
      const begin = await this.httpJson("POST", "/rpc", { cmd: "begin-bind-agents-window" });
      if (!begin || begin.ok === false) {
        this.notify(`Bind failed: ${String((begin && begin.error) || "begin failed")}`, true);
        return;
      }
      this.notify("Open Cursor → File → New Agents Window, then click OK");
      window.alert(
        "In Cursor Desktop:\nFile → New Agents Window\n\nWhen the Agents Window is open, click OK to bind it."
      );
      const done = await this.httpJson("POST", "/rpc", { cmd: "complete-bind-agents-window" });
      if (!done || done.ok === false) {
        const err = (done && done.error) || "complete failed";
        this.notify(`Bind failed: ${String(err).slice(0, 200)}`, true);
        await this.refreshAgentsPaneUi();
        return;
      }
      this.notify("Agents Window bound");
      await this.refreshAgentsPaneUi();
    } catch (err) {
      this.notify(`Bind error: ${err.message || err}`, true);
    }
  }

  async focusAgentsWindow() {
    try {
      if (!(await this.daemonHealthy())) return;
      const result = await this.httpJson("POST", "/rpc", { cmd: "focus-agents-window" });
      if (!result || result.ok === false) {
        this.notify(`Focus Agents failed: ${String((result && result.error) || "rpc")}`, true);
        return;
      }
      this.notify("Focused Agents Window");
    } catch (err) {
      this.notify(`Focus Agents error: ${err.message || err}`, true);
    }
  }

  async openEmbeddedPane() {
    // Backward-compatible alias → native one-click flow
    await this.openNativeAgentsPane();
  }

  async ensureAgentsPaneLeaf() {
    let leaf = this.app.workspace.getLeavesOfType(VIEW_TYPE_EMBEDDED_PANE)[0];
    if (!leaf) {
      leaf = this.app.workspace.getRightLeaf(false);
    }
    if (!leaf) return null;
    try {
      if (this.app.workspace.rightSplit) {
        this.app.workspace.rightSplit.expand();
      }
    } catch (_e) {
      /* ignore */
    }
    await leaf.setViewState({ type: VIEW_TYPE_EMBEDDED_PANE, active: true });
    this.app.workspace.revealLeaf(leaf);
    await sleep(50);
    return leaf;
  }

  async openNativeAgentsPane() {
    if (!this.settings.embeddedPaneExperimental) {
      this.notify("Enable Embedded Agents Pane [Experimental] in settings first.", true);
      return;
    }
    this.settings.embedBackend = "native_child";
    await this.saveSettings();
    const attached = await this.ensureAttachedForEmbed();
    if (!attached) {
      this.notify("Attach Cursor Sidecar first.", true);
      return;
    }
    const leaf = await this.ensureAgentsPaneLeaf();
    if (!leaf) {
      this.notify("Could not open right sidebar leaf", true);
      return;
    }
    const view = this._embeddedPaneView;
    if (!view) {
      this.notify("Agents Pane view missing", true);
      return;
    }
    view.renderPaneUi("opening");
    const up = await this.ensureDaemon();
    if (!up) {
      view.renderPaneUi("failed", "daemon unavailable");
      this.notify("Cursor Sidecar: daemon unavailable", true);
      return;
    }
    try {
      await this.httpJson("POST", "/rpc", {
        cmd: "set-embed-backend",
        backend: "native_child",
      });
    } catch (_e) {
      /* ignore */
    }
    await sleep(40);
    const pane = view.getPaneRectPayload();
    pane.backend = "native_child";
    pane.chrome_level = "full";
    if (this.settings.borderlessCursorExperimental) {
      pane.borderless = true;
    }
    try {
      const result = await this.httpJson("POST", "/rpc", {
        cmd: "open-native-agents-pane",
        pane,
        backend: "native_child",
      });
      if (!result || result.ok === false) {
        this._embedActive = false;
        this._nativeVerified = false;
        const detail = this._formatNativeFail(result);
        view.renderPaneUi("failed", detail);
        this.notify(`Native Agents Pane FAILED (no visual fallback): ${detail}`, true);
        return;
      }
      const emb = result.embedded_status || {};
      const verified =
        !!result.is_native_child_verified ||
        !!emb.native_child ||
        !!emb.is_native_child;
      if (!verified) {
        this._embedActive = false;
        this._nativeVerified = false;
        const detail = this._formatNativeFail(result);
        view.renderPaneUi("failed", detail);
        this.notify(`Native NOT verified: ${detail}`, true);
        return;
      }
      this._embedActive = true;
      this._nativeVerified = true;
      view.renderPaneUi("anchor");
      this._lastEmbedDiag = {
        dom: pane,
        placement: result.placement,
        parent: result.agent_parent_hwnd,
        WS_CHILD: result.WS_CHILD,
        WS_POPUP: result.WS_POPUP,
        style_after: result.style_after || emb.style_after,
        verified: true,
        auto_bind: result.auto_bind,
      };
      this.notify(
        `Native Agents Pane OK hwnd=${result.agent_hwnd} parent=${result.agent_parent_hwnd}`
      );
      this.schedulePaneRectUpdate();
    } catch (err) {
      this._embedActive = false;
      this._nativeVerified = false;
      view.renderPaneUi("failed", String(err.message || err));
      this.notify(`Native Agents Pane error: ${err.message || err}`, true);
    }
  }

  async closeEmbeddedPane() {
    await this.exitEmbeddedMode({ closePane: true });
  }

  async toggleEmbeddedMode() {
    if (this._embedActive) {
      await this.exitEmbeddedMode({ closePane: false });
      return;
    }
    await this.openNativeAgentsPane();
  }

  _formatNativeFail(result) {
    const emb = (result && result.embedded_status) || {};
    const place = (result && result.placement) || {};
    const after = place.after || {};
    const parent = emb.agent_parent_hwnd ?? after.parent_hwnd ?? place.parent_hwnd;
    const expected = emb.expected_parent_hwnd ?? place.expected_parent_hwnd;
    const bits = [
      `err=${(result && result.error) || "native_child_enter_failed"}`,
      `win32=${result && result.win32_error != null ? result.win32_error : "?"}`,
      `GetParent=${parent ?? "?"}`,
      `expected=${expected ?? "?"}`,
      `WS_CHILD=${emb.WS_CHILD ?? after.WS_CHILD ?? place.WS_CHILD}`,
      `WS_POPUP=${emb.WS_POPUP ?? after.WS_POPUP ?? place.WS_POPUP}`,
      `verified=false`,
    ];
    return bits.join(" ");
  }

  async enterEmbeddedFromPane(view) {
    if (!this.settings.embeddedPaneExperimental) {
      this.notify("Enable Embedded Agents Pane [Experimental] in settings first.", true);
      return;
    }
    const attached = await this.ensureAttachedForEmbed();
    if (!attached) {
      this.notify("Attach Cursor Sidecar first.", true);
      return;
    }
    if (!view) {
      this.notify("Open Agents Pane first.", true);
      return;
    }
    const up = await this.ensureDaemon();
    if (!up) {
      this.notify("Cursor Sidecar: daemon unavailable for embed", true);
      return;
    }
    await sleep(40);
    const pane = view.getPaneRectPayload();
    if (this.settings.borderlessCursorExperimental) {
      pane.borderless = true;
    }
    const useNative = this.settings.embedBackend === "native_child";
    if (useNative) {
      pane.backend = "native_child";
    }
    try {
      const result = await this.httpJson("POST", "/rpc", {
        cmd: useNative ? "enter-native-agents-embed" : "enter-embedded-pane",
        pane,
        backend: useNative ? "native_child" : undefined,
      });
      if (!result || result.ok === false) {
        const err = (result && result.error) || "enter-embedded-pane failed";
        this._embedActive = false;
        this._nativeVerified = false;
        if (err === "agent_not_bound") {
          this.notify("Bind Agents Window first.", true);
          if (view.renderPaneUi) view.renderPaneUi("unbound");
          return;
        }
        if (useNative) {
          this.notify(`Native embed FAILED (no visual fallback): ${this._formatNativeFail(result)}`, true);
          if (view.renderPaneUi) view.renderPaneUi("ready");
          return;
        }
        this.notify(`Embed failed: ${String(err).slice(0, 200)}`, true);
        return;
      }
      if (useNative) {
        const emb = result.embedded_status || {};
        const verified =
          !!result.is_native_child_verified ||
          !!emb.native_child ||
          !!emb.is_native_child;
        if (!verified) {
          this._embedActive = false;
          this._nativeVerified = false;
          this.notify(`Native embed NOT verified: ${this._formatNativeFail(result)}`, true);
          if (view.renderPaneUi) view.renderPaneUi("ready");
          return;
        }
        this._embedActive = true;
        this._nativeVerified = true;
        view.renderPaneUi("anchor");
        this._lastEmbedDiag = {
          dom: pane,
          placement: result.placement,
          parent: result.agent_parent_hwnd,
          WS_CHILD: result.WS_CHILD,
          WS_POPUP: result.WS_POPUP,
          verified: true,
        };
        this.notify(
          `Native child OK GetParent=${result.agent_parent_hwnd} WS_CHILD=${result.WS_CHILD} WS_POPUP=${result.WS_POPUP}`
        );
        this.schedulePaneRectUpdate();
        return;
      }
      this._embedActive = true;
      this._nativeVerified = false;
      view.renderPaneUi("embedded");
      this._lastEmbedDiag = { dom: pane, placement: result.placement };
      this.notify("Agents Window embedded in pane (visual)");
      this.schedulePaneRectUpdate();
    } catch (err) {
      this._embedActive = false;
      this._nativeVerified = false;
      this.notify(`Embed error: ${err.message || err}`, true);
    }
  }

  async exitEmbeddedMode({ closePane = false, fromViewClose = false } = {}) {
    const wasActive = this._embedActive;
    this._embedActive = false;
    this._nativeVerified = false;
    try {
      if (wasActive && (await this.daemonHealthy())) {
        await this.httpJson("POST", "/rpc", { cmd: "exit-embedded-pane" });
      }
    } catch (_e) {
      /* ignore */
    }
    await this.refreshAgentsPaneUi();
    if (closePane && !fromViewClose) {
      const leaves = this.app.workspace.getLeavesOfType(VIEW_TYPE_EMBEDDED_PANE);
      for (const leaf of leaves) {
        leaf.detach();
      }
    }
    if (wasActive && !fromViewClose) {
      this.notify("Exited Agents Pane (Editor unchanged)");
    }
  }

  async enterNativeAgentsEmbed() {
    await this.openNativeAgentsPane();
  }

  async recoverNativeAgents() {
    try {
      const up = await this.ensureDaemon();
      if (!up) {
        this.notify("Daemon unavailable", true);
        return;
      }
      const result = await this.httpJson("POST", "/rpc", { cmd: "recover-native-child" });
      if (!result || result.ok === false) {
        this.notify(`Recover failed: ${String((result && result.error) || "rpc")}`, true);
        return;
      }
      this._embedActive = false;
      this.notify("Native Agents Window recovered");
      await this.refreshAgentsPaneUi();
    } catch (err) {
      this.notify(`Recover error: ${err.message || err}`, true);
    }
  }

  async tempHideAgentsChild() {
    if (!this._embedActive || !this._embeddedPaneView) return;
    const pane = this._embeddedPaneView.getPaneRectPayload();
    pane.visible = false;
    try {
      await this.httpJson("POST", "/rpc", { cmd: "update-embedded-pane", pane });
      this.notify("Agents child temporarily hidden");
    } catch (_e) {
      /* ignore */
    }
  }

  vaultPath() {
    const adapter = this.app.vault.adapter;
    if (adapter && typeof adapter.getBasePath === "function") {
      return adapter.getBasePath();
    }
    return adapter && adapter.basePath ? adapter.basePath : "";
  }

  /** Folder Cursor should treat as the fixed project (Obsidian vault by default). */
  cursorProjectPath() {
    const fixed = (this.settings.cursorProjectFolder || "").trim();
    if (fixed) return fixed;
    return this.vaultPath();
  }

  resolvePluginDir() {
    // Prefer Obsidian-provided absolute plugin dir when available
    if (this.manifest && typeof this.manifest.dir === "string" && this.manifest.dir) {
      const dir = this.manifest.dir;
      if (path.isAbsolute(dir) && fs.existsSync(dir)) return dir;
      const base = this.vaultPath();
      if (base) {
        const joined = path.join(base, dir);
        if (fs.existsSync(joined)) return joined;
      }
    }
    const base = this.vaultPath();
    const configDir = (this.app.vault && this.app.vault.configDir) || ".obsidian";
    const id = (this.manifest && this.manifest.id) || "cursor-sidecar";
    return path.join(base, configDir, "plugins", id);
  }

  resolveHelper() {
    const pluginDir = this.resolvePluginDir();
    const packaged = path.join(pluginDir, "bin", "cursor-sidecar.exe");
    if (fs.existsSync(packaged)) {
      return {
        mode: "packaged",
        command: packaged,
        argsPrefix: [],
        cwd: pluginDir,
        binaryFound: true,
      };
    }
    const dir = (this.settings.sidecarDir || "").trim();
    const mainPy = path.join(dir, "main.py");
    const py = this.resolvePython();
    return {
      mode: "developer",
      command: py,
      argsPrefix: [mainPy],
      cwd: dir || undefined,
      binaryFound: false,
      mainPy,
    };
  }

  resolvePython() {
    const configured = (this.settings.pythonPath || "").trim();
    if (configured && fs.existsSync(configured)) return configured;
    const dir = (this.settings.sidecarDir || "").trim();
    if (dir) {
      const venvPy = path.join(dir, ".venv", "Scripts", "python.exe");
      if (fs.existsSync(venvPy)) return venvPy;
    }
    return "python";
  }

  resolveDataDir() {
    if (this._runtimeInfo && this._runtimeInfo.data_dir) {
      return String(this._runtimeInfo.data_dir);
    }
    const override = (this.settings.dataDirOverride || "").trim();
    if (override) return override;
    const envOverride = (process.env.CURSOR_SIDECAR_DATA_DIR || "").trim();
    if (envOverride) return envOverride;
    const helper = this.resolveHelper();
    if (helper.mode === "packaged") {
      const local = process.env.LOCALAPPDATA;
      if (local) return path.join(local, "CursorSidecar");
      return path.join(process.env.USERPROFILE || "", "AppData", "Local", "CursorSidecar");
    }
    return (this.settings.sidecarDir || "").trim();
  }

  helperConfigured() {
    const helper = this.resolveHelper();
    if (helper.mode === "packaged") return true;
    const dir = (this.settings.sidecarDir || "").trim();
    if (!dir) {
      this.notify(
        "Cursor Sidecar: enable Developer mode and set Sidecar source directory, or install the packaged release.",
        true
      );
      return false;
    }
    if (!fs.existsSync(helper.mainPy)) {
      this.notify(`Cursor Sidecar: main.py not found in ${dir}`, true);
      return false;
    }
    return true;
  }

  /** @deprecated use helperConfigured */
  pathsConfigured() {
    return this.helperConfigured();
  }

  resolveDaemonToken() {
    const dataDir = this.resolveDataDir();
    if (!dataDir) return "";
    const tokenPath = path.join(dataDir, ".sidecar.daemon.token");
    try {
      if (fs.existsSync(tokenPath)) {
        return fs.readFileSync(tokenPath, "utf8").trim();
      }
    } catch (_e) {
      /* ignore */
    }
    return "";
  }

  majorMinor(ver) {
    const parts = String(ver || "")
      .trim()
      .split(".")
      .map((x) => Number(x));
    return `${parts[0] || 0}.${parts[1] || 0}`;
  }

  async checkHelperVersionMismatch() {
    const helper = this.resolveHelper();
    if (helper.mode !== "packaged") return;
    try {
      const result = await this.spawnAsync(helper.command, ["--version"], helper.cwd);
      if (result.code !== 0) return;
      const helperVer = (result.stdout || "").trim().split(/\r?\n/)[0].trim();
      const pluginVer = (this.manifest && this.manifest.version) || "";
      if (helperVer && pluginVer && this.majorMinor(helperVer) !== this.majorMinor(pluginVer)) {
        this.notify("Cursor Sidecar helper version mismatch", true);
      }
    } catch (_e) {
      /* ignore */
    }
  }

  async probeHelperVersion() {
    const helper = this.resolveHelper();
    if (helper.mode !== "packaged") return null;
    try {
      const result = await this.spawnAsync(helper.command, ["--version"], helper.cwd);
      if (result.code !== 0) return null;
      return (result.stdout || "").trim().split(/\r?\n/)[0].trim() || null;
    } catch (_e) {
      return null;
    }
  }

  getCurrentEditorContext() {
    const adapter = this.app.vault.adapter;
    if (!(adapter instanceof FileSystemAdapter) && typeof adapter.getBasePath !== "function") {
      if (!adapter || typeof adapter.basePath !== "string") {
        return { ok: false, error: "desktop_filesystem_required" };
      }
    }
    const file = this.app.workspace.getActiveFile();
    if (!file) {
      return { ok: false, error: "no_active_file" };
    }
    const vaultRoot = this.vaultPath();
    const absPath = path.join(vaultRoot, file.path);
    let line = null;
    let column = null;
    const view = this.app.workspace.getActiveViewOfType(MarkdownView);
    if (view && view.editor) {
      const cur = view.editor.getCursor();
      line = cur.line + 1;
      column = cur.ch + 1;
    }
    return {
      ok: true,
      vaultRoot,
      path: absPath,
      relativePath: file.path,
      line,
      column,
    };
  }

  async openCurrentNoteInCursor() {
    const ctx = this.getCurrentEditorContext();
    if (!ctx.ok) {
      if (ctx.error === "desktop_filesystem_required") {
        this.notify("Cursor Sidecar: Desktop filesystem vault required.", true);
      } else {
        this.notify("Cursor Sidecar: no active note to open.", true);
      }
      return null;
    }
    return await this.openAbsoluteInCursor(ctx.vaultRoot, ctx.path, ctx.line, ctx.column);
  }

  async openVaultInCursor() {
    if (!this.helperConfigured()) return null;
    const vaultRoot = this.cursorProjectPath();
    this.notify("Cursor Sidecar: opening vault in Cursor…");
    try {
      const up = await this.ensureDaemon();
      if (!up) {
        this.notify("Cursor Sidecar: daemon unavailable", true);
        return null;
      }
      const result = await this.httpJson("POST", "/rpc", {
        cmd: "open-editor-vault",
        path: vaultRoot,
        focus: true,
      });
      if (!result || result.ok === false) {
        const err = (result && result.error) || "rpc failed";
        if (err === "sidecar_not_attached") {
          this.notify("Attach Cursor Sidecar first.", true);
        } else {
          this.notify(`Open vault failed: ${String(err).slice(0, 200)}`, true);
        }
        return null;
      }
      this.notify(`Opened vault in Cursor (${result.method || "ok"})`);
      return result;
    } catch (err) {
      this.notify(`Open vault error: ${err.message || err}`, true);
      return null;
    }
  }

  async openFileInCursor(tfile, line0, ch0) {
    if (!tfile) {
      this.notify("No file", true);
      return null;
    }
    const vaultRoot = this.vaultPath();
    const absPath = path.join(vaultRoot, tfile.path);
    let line = null;
    let column = null;
    if (line0 !== null && line0 !== undefined) {
      line = line0 + 1;
      column = ch0 !== null && ch0 !== undefined ? ch0 + 1 : 1;
    }
    return await this.openAbsoluteInCursor(vaultRoot, absPath, line, column);
  }

  async openAbsoluteInCursor(vaultRoot, absPath, line, column) {
    if (!this.helperConfigured()) return null;
    this.notify("Cursor Sidecar: opening note in Cursor…");
    try {
      const up = await this.ensureDaemon();
      if (!up) {
        this.notify("Cursor Sidecar: daemon unavailable", true);
        return null;
      }
      const body = {
        cmd: "open-editor-file",
        vault_root: vaultRoot,
        path: absPath,
        focus: true,
      };
      if (line !== null && line !== undefined) body.line = line;
      if (column !== null && column !== undefined) body.column = column;
      const result = await this.httpJson("POST", "/rpc", body);
      if (!result || result.ok === false) {
        const err = (result && result.error) || "rpc failed";
        if (err === "sidecar_not_attached") {
          this.notify("Attach Cursor Sidecar first.", true);
        } else {
          this.notify(`Open in Cursor failed: ${String(err).slice(0, 200)}`, true);
        }
        return null;
      }
      const where =
        result.line != null
          ? `${result.path}:${result.line}${result.column != null ? ":" + result.column : ""}`
          : result.path;
      const warn = result.routing_warning ? ` (${result.routing_warning})` : "";
      this.notify(`Opened in Cursor via ${result.method}: ${String(where).slice(0, 180)}${warn}`);
      return result;
    } catch (err) {
      this.notify(`Open in Cursor error: ${err.message || err}`, true);
      return null;
    }
  }

  isExcludedFollowPath(relPath) {
    const raw = String(relPath || "").replace(/\\/g, "/").replace(/^\/+/, "");
    if (!raw) return true;
    const cfg = String((this.app.vault && this.app.vault.configDir) || ".obsidian").replace(/\\/g, "/");
    const parts = raw.split("/").filter((p) => p && p !== ".");
    if (!parts.length) return true;
    if (parts[0] === cfg || parts[0] === ".obsidian") return true;
    if (parts[0].startsWith(".")) return true;
    return false;
  }

  notifyContextFollowError(key, message) {
    const now = Date.now();
    if (key === this._cfLastErrorKey && now - (this._cfLastErrorAt || 0) < 30000) {
      return;
    }
    this._cfLastErrorKey = key;
    this._cfLastErrorAt = now;
    this.notify(message, true);
  }

  async clearContextSyncPending() {
    try {
      if (await this.daemonHealthy()) {
        await this.httpJson("POST", "/rpc", { cmd: "context-sync-clear" });
      }
    } catch (_e) {
      /* ignore */
    }
  }

  onContextFollowFileOpen(file) {
    if (!this.settings.contextFollow) return;
    if (!(file instanceof TFile)) return;
    if (this.isExcludedFollowPath(file.path)) return;

    if (this._cfTimer) clearTimeout(this._cfTimer);
    this._cfGen += 1;
    const gen = this._cfGen;
    const target = file;
    this._cfTimer = setTimeout(() => {
      this._cfTimer = null;
      if (gen !== this._cfGen) return;
      this.runContextFollowSync(target).catch(() => {});
    }, CONTEXT_FOLLOW_DEBOUNCE_MS);
  }

  async runContextFollowSync(file) {
    if (!this.settings.contextFollow) return null;
    if (!(file instanceof TFile)) return null;
    if (this.isExcludedFollowPath(file.path)) return null;
    if (!this.helperConfigured()) return null;

    const vaultRoot = this.vaultPath();
    const absPath = path.join(vaultRoot, file.path);
    const now = Date.now();
    let line = null;
    let column = null;
    const isMd = file.extension === "md" || file.extension === "markdown";
    if (isMd) {
      const view = this.app.workspace.getActiveViewOfType(MarkdownView);
      if (view && view.file && view.file.path === file.path && view.editor) {
        const cur = view.editor.getCursor();
        line = cur.line + 1;
        column = cur.ch + 1;
      }
    }

    // Same-file suppression (auto only — manual Open never uses this path)
    if (
      this._cfLastPath === absPath &&
      this._cfLastLine === line &&
      now - (this._cfLastSyncAt || 0) < CONTEXT_FOLLOW_SUPPRESS_MS
    ) {
      return null;
    }

    try {
      let up = this._cfDaemonOk ? await this.daemonHealthy() : false;
      if (!up) {
        up = await this.ensureDaemon();
        this._cfDaemonOk = !!up;
      }
      if (!up) {
        this.notifyContextFollowError("daemon", "Cursor Sidecar: daemon unavailable");
        return null;
      }
      const st = await this.httpJson("GET", "/status").catch(() => null);
      const status = (st && st.status) || st || {};
      if (!status.attached) return null;
      if (status.cursor_binding && status.cursor_binding.ok === false) {
        this.notifyContextFollowError("binding", "Cursor Sidecar: binding gone — Context Follow idle");
        return null;
      }

      const daemonSeq = Number(
        (status.context_sync && status.context_sync.latest_seq) || 0
      );
      this._cfSeq = nextContextSyncSeq(this._cfSeq, daemonSeq, Date.now());

      const body = {
        cmd: "sync-editor-file",
        vault_root: vaultRoot,
        path: absPath,
        relative_path: file.path,
        seq: this._cfSeq,
      };
      if (line !== null) body.line = line;
      if (column !== null) body.column = column;

      let result = await this.httpJson("POST", "/rpc", body);

      // Stale seq after plugin reload: rebase once and retry (never infinite)
      if (result && result.discarded === true && result.reason === "stale_seq") {
        const latest = Number(result.latest_seq || daemonSeq || 0);
        this._cfSeq = nextContextSyncSeq(this._cfSeq, latest, Date.now());
        body.seq = this._cfSeq;
        result = await this.httpJson("POST", "/rpc", body);
      }

      if (!result || result.ok === false) {
        const err = (result && result.error) || "sync failed";
        if (err === "sidecar_not_attached" || err === "stale_binding") {
          this.notifyContextFollowError(err, `Cursor Sidecar: ${err}`);
        }
        return null;
      }

      // ok=true + discarded must not count as a successful sync
      if (result.discarded === true || result.queued === false) {
        return null;
      }

      // queued=true (or legacy helpers without queued field after real open)
      this._cfLastPath = absPath;
      this._cfLastLine = line;
      this._cfLastSyncAt = Date.now();
      this._cfLastRelative = file.path;
      return result;
    } catch (_e) {
      return null;
    }
  }

  async ensureRuntimeInfo({ force = false } = {}) {
    if (this._runtimeInfo && !force) return this._runtimeInfo;
    if (!this.helperConfigured()) return null;
    try {
      const result = await this.runHelper(["runtime-info", "--json"]);
      const out = (result.stdout || "").trim();
      const line = out.split(/\r?\n/).filter(Boolean).pop() || "";
      const info = JSON.parse(line);
      if (info && info.data_dir) {
        this._runtimeInfo = info;
        if (info.daemon_host) this.settings.daemonHost = String(info.daemon_host);
        if (info.daemon_port) this.settings.daemonPort = Number(info.daemon_port) || this.settings.daemonPort;
        return info;
      }
    } catch (_e) {
      /* ignore */
    }
    return null;
  }

  helperFrontArgs() {
    const dataDir = this.resolveDataDir();
    const args = [];
    if (dataDir) {
      args.push("--data-dir", dataDir);
    }
    return args;
  }

  async withCommandLifecycle(label, fn, { timeoutMs = 15000 } = {}) {
    if (this._commandBusy) {
      this.notify("Cursor Sidecar: busy — wait for previous command", true);
      return null;
    }
    this._commandBusy = true;
    const notice = this.settings.showNotices ? new Notice(`Cursor Sidecar: ${label}…`, 0) : null;
    let timer = null;
    try {
      const work = Promise.resolve().then(() => fn());
      const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`timeout after ${timeoutMs}ms`)), timeoutMs);
      });
      const result = await Promise.race([work, timeout]);
      if (notice) notice.hide();
      return result;
    } catch (err) {
      if (notice) notice.hide();
      this.notify(`Cursor Sidecar: ${label} failed — ${err.message || err}`, true);
      return null;
    } finally {
      if (timer) clearTimeout(timer);
      this._commandBusy = false;
    }
  }

  async ensureDaemon() {
    if (await this.daemonHealthy()) return true;
    if (!this.helperConfigured()) return false;
    await this.ensureRuntimeInfo({ force: !this._runtimeInfo });
    const host = this.settings.daemonHost || "127.0.0.1";
    const port = String(Number(this.settings.daemonPort) || 27845);
    const deadline = Date.now() + 15000;
    try {
      await this.runHelper(["daemon-start", "--host", host, "--port", port]);
      while (Date.now() < deadline) {
        await sleep(250);
        if (await this.daemonHealthy()) return true;
      }
    } catch (_e) {
      return false;
    }
    return false;
  }

  async applyLiveSidecarSetting(enabled) {
    this.settings.liveSidecar = !!enabled;
    await this.saveSettings();
    try {
      if (enabled) {
        const up = await this.ensureDaemon();
        if (!up) {
          this.notify("Cursor Sidecar: could not start daemon for Live Sidecar", true);
          return;
        }
        const result = await this.httpJson("POST", "/rpc", {
          cmd: "set-live-follow",
          enabled: true,
        });
        if (!result || result.ok === false) {
          this.notify(
            `Cursor Sidecar: set-live-follow failed: ${String(
              (result && result.error) || "rpc failed"
            ).slice(0, 200)}`,
            true
          );
          return;
        }
        this.notify("Live Sidecar ON");
      } else if (await this.daemonHealthy()) {
        const result = await this.httpJson("POST", "/rpc", {
          cmd: "set-live-follow",
          enabled: false,
        });
        if (!result || result.ok === false) {
          this.notify(
            `Cursor Sidecar: set-live-follow failed: ${String(
              (result && result.error) || "rpc failed"
            ).slice(0, 200)}`,
            true
          );
          return;
        }
        this.notify("Live Sidecar OFF (windows stay; follow stopped)");
      }
    } catch (err) {
      this.notify(`Live Sidecar toggle error: ${err.message || err}`, true);
    }
  }

  daemonHealthy() {
    return this.httpJson("GET", "/health")
      .then((r) => r && r.ok)
      .catch(() => false);
  }

  httpJson(method, urlPath, body) {
    const host = this.settings.daemonHost || "127.0.0.1";
    const port = Number(this.settings.daemonPort) || 27845;
    const token = this.resolveDaemonToken();
    const payload = body ? JSON.stringify(body) : null;
    const headers = {
      "X-Cursor-Sidecar-Token": token,
    };
    if (payload) {
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = Buffer.byteLength(payload);
    }
    return new Promise((resolve, reject) => {
      const req = http.request(
        {
          host,
          port,
          path: urlPath,
          method,
          headers,
          timeout: 20000,
        },
        (res) => {
          let data = "";
          res.on("data", (c) => (data += c));
          res.on("end", () => {
            if (res.statusCode === 403) {
              reject(new Error("daemon auth failed (403) — check .sidecar.daemon.token"));
              return;
            }
            try {
              resolve(JSON.parse(data || "{}"));
            } catch (e) {
              reject(e);
            }
          });
        }
      );
      req.on("error", reject);
      req.on("timeout", () => {
        req.destroy();
        reject(new Error("daemon timeout"));
      });
      if (payload) req.write(payload);
      req.end();
    });
  }

  buildRpcBody(cmd) {
    const body = { cmd };
    if (
      this.settings.openVaultInCursor &&
      (cmd === "click" || cmd === "attach" || cmd === "dock")
    ) {
      body.workspace = this.cursorProjectPath();
    }
    return body;
  }

  async runAction(cmd, showRaw = false) {
    if (!this.helperConfigured()) return null;
    return await this.withCommandLifecycle(cmd, async () => {
      await this.ensureRuntimeInfo();
      if (this.settings.liveSidecar) {
        const up = await this.ensureDaemon();
        if (up) {
          const result =
            cmd === "status"
              ? await this.httpJson("GET", "/status")
              : await this.httpJson("POST", "/rpc", this.buildRpcBody(cmd));
          if (!result || result.ok === false) {
            const err = (result && (result.error || JSON.stringify(result))) || "rpc failed";
            this.notify(`Cursor Sidecar failed: ${String(err).slice(0, 300)}`, true);
            return null;
          }
          if (showRaw || cmd === "status") {
            this.notify(`Cursor Sidecar: ${JSON.stringify(result.status || result).slice(0, 280)}`);
          } else if (cmd === "compact" || cmd === "normal" || cmd === "wide") {
            this.notify(`Cursor Sidecar: preset ${cmd}`);
          } else if (result.attached === true) {
            this.notify("Cursor Sidecar: attached");
          } else if (result.attached === false) {
            this._embedActive = false;
            this.notify("Cursor Sidecar: detached (windows restored)");
          } else {
            this.notify(`Cursor Sidecar: ${cmd} ok`);
          }
          if (cmd === "detach") {
            this._embedActive = false;
          }
          return result;
        }
        this.notify("Cursor Sidecar: daemon unavailable — falling back to one-shot CLI", true);
      }
      return await this.runHelperCommand(cmd, showRaw);
    }, { timeoutMs: 20000 });
  }

  async runHelper(args) {
    const helper = this.resolveHelper();
    const front = this.helperFrontArgs();
    const finalArgs = [...helper.argsPrefix, ...front, ...args];
    return this.spawnAsync(helper.command, finalArgs, helper.cwd);
  }

  async runHelperCommand(command, returnStdout = false) {
    const extra = [];
    if (
      this.settings.openVaultInCursor &&
      (command === "click" || command === "attach" || command === "dock")
    ) {
      extra.push("--workspace", this.cursorProjectPath());
    }
    const cmdArgs =
      command === "status" ? [...extra, "status", "--json"] : [...extra, command];
    const result = await this.runHelper(cmdArgs);
    if (result.code !== 0) {
      const errText = (result.stderr || result.stdout || `exit ${result.code}`).trim();
      this.notify(`Cursor Sidecar failed: ${errText.slice(0, 300)}`, true);
      return null;
    }
    const out = (result.stdout || "").trim();
    if (returnStdout || command === "status") {
      this.notify(`Cursor Sidecar: ${out.slice(0, 280)}`);
    } else if (out) {
      const lines = out.split(/\r?\n/).filter(Boolean);
      const last = lines[lines.length - 1];
      this.notify(last || `Cursor Sidecar: ${command} ok`);
    } else {
      this.notify(`Cursor Sidecar: ${command} ok`);
    }
    return out;
  }

  spawnAsync(exe, args, cwd) {
    return new Promise((resolve, reject) => {
      const child = spawn(exe, args, {
        cwd: cwd || undefined,
        windowsHide: true,
        shell: false,
      });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (d) => {
        stdout += d.toString();
      });
      child.stderr.on("data", (d) => {
        stderr += d.toString();
      });
      child.on("error", reject);
      child.on("close", (code) => resolve({ code: code ?? 1, stdout, stderr }));
    });
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

class CursorSidecarSettingTab extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display() {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl("h2", { text: "Cursor Sidecar" });
    containerEl.createEl("p", {
      text: "Attach real Cursor Desktop beside Obsidian. With Live Sidecar on, Cursor follows Obsidian while attached.",
    });

    const helper = this.plugin.resolveHelper();
    const status = containerEl.createDiv({ cls: "cursor-sidecar-status" });
    status.createEl("h3", { text: "Status" });
    status.createEl("p", {
      text: `Helper: ${helper.mode === "packaged" ? "Packaged" : "Developer"}`,
    });
    status.createEl("p", {
      text: `Binary: ${helper.mode === "packaged" && helper.binaryFound ? "Found" : helper.mode === "packaged" ? "Missing" : "n/a (source)"}`,
    });
    status.createEl("p", {
      text: `Version: ${(this.plugin.manifest && this.plugin.manifest.version) || "0.7.2-dev"}`,
    });
    const daemonLine = status.createEl("p", { text: "Daemon: …" });
    this.plugin
      .daemonHealthy()
      .then((ok) => {
        daemonLine.setText(`Daemon: ${ok ? "Running" : "Stopped"}`);
      })
      .catch(() => {
        daemonLine.setText("Daemon: Stopped");
      });
    this.plugin.probeHelperVersion().then((ver) => {
      if (ver) {
        status.createEl("p", { text: `Helper version: ${ver}` });
      }
    });

    new Setting(containerEl)
      .setName("Live Sidecar")
      .setDesc("When enabled, Cursor follows Obsidian while attached. Off keeps the layout but stops live follow.")
      .addToggle((toggle) =>
        toggle.setValue(!!this.plugin.settings.liveSidecar).onChange(async (value) => {
          await this.plugin.applyLiveSidecarSetting(value);
        })
      );

    new Setting(containerEl)
      .setName("Context Follow")
      .setDesc("Automatically keep the bound Cursor Editor on the active Obsidian file (silent; does not steal focus). Requires Attach.")
      .addToggle((toggle) =>
        toggle.setValue(!!this.plugin.settings.contextFollow).onChange(async (value) => {
          this.plugin.settings.contextFollow = !!value;
          await this.plugin.saveSettings();
          this.plugin._cfGen += 1;
          if (this.plugin._cfTimer) {
            clearTimeout(this.plugin._cfTimer);
            this.plugin._cfTimer = null;
          }
          if (!value) {
            await this.plugin.clearContextSyncPending();
          }
          // ON: wait for next file-open (no immediate sync)
          this.display();
        })
      );

    new Setting(containerEl)
      .setName("Embedded Agents Pane [Experimental]")
      .setDesc(
        "Places Cursor's New Agents Window over an Obsidian pane. Visual embed only — not the full Editor. Editor stays for Context Follow."
      )
      .addToggle((toggle) =>
        toggle.setValue(!!this.plugin.settings.embeddedPaneExperimental).onChange(async (value) => {
          this.plugin.settings.embeddedPaneExperimental = !!value;
          await this.plugin.saveSettings();
          if (!value && this.plugin._embedActive) {
            await this.plugin.exitEmbeddedMode({ closePane: true });
          }
          this.display();
        })
      );

    new Setting(containerEl)
      .setName("Borderless Agents Window [Experimental]")
      .setDesc("When embedding, strip Agents Window caption/thickframe only (not Editor). Default OFF.")
      .addToggle((toggle) =>
        toggle.setValue(!!this.plugin.settings.borderlessCursorExperimental).onChange(async (value) => {
          this.plugin.settings.borderlessCursorExperimental = !!value;
          await this.plugin.saveSettings();
        })
      );

    new Setting(containerEl)
      .setName("Agents Embed Backend")
      .setDesc(
        this.plugin.settings.embedBackend === "native_child"
          ? "WARNING: Reparents the Cursor Agents Window into Obsidian using Win32 SetParent. Cursor/Electron updates may break this."
          : "Visual (recommended) = SetWindowPos overlay. Native Child = highly experimental SetParent."
      )
      .addDropdown((dd) =>
        dd
          .addOption("visual", "Visual (recommended)")
          .addOption("native_child", "Native Child (highly experimental)")
          .setValue(this.plugin.settings.embedBackend === "native_child" ? "native_child" : "visual")
          .onChange(async (value) => {
            this.plugin.settings.embedBackend = value === "native_child" ? "native_child" : "visual";
            await this.plugin.saveSettings();
            try {
              if (await this.plugin.daemonHealthy()) {
                await this.plugin.httpJson("POST", "/rpc", {
                  cmd: "set-embed-backend",
                  backend: this.plugin.settings.embedBackend,
                });
              }
            } catch (_e) {
              /* ignore */
            }
            this.display();
          })
      );

    status.createEl("p", {
      text: `Context Follow: ${this.plugin.settings.contextFollow ? "On" : "Off"}`,
    });
    status.createEl("p", {
      text: `Embedded Pane: ${this.plugin.settings.embeddedPaneExperimental ? "On" : "Off"}${
        this.plugin._embedActive ? " (active)" : ""
      }${this.plugin._nativeVerified ? " native=verified" : ""}`,
    });
    if (this.plugin.settings.developerMode && this.plugin._lastEmbedDiag) {
      const d = this.plugin._lastEmbedDiag;
      const pl = d.placement || {};
      status.createEl("p", {
        text: `Embed diag: mode=${this.plugin._embedActive ? "pane" : "sidecar"} verified=${
          d.verified != null ? d.verified : this.plugin._nativeVerified
        } parent=${d.parent != null ? d.parent : "?"} WS_CHILD=${
          d.WS_CHILD != null ? d.WS_CHILD : "?"
        } WS_POPUP=${d.WS_POPUP != null ? d.WS_POPUP : "?"} visible=${
          pl.visible != null ? pl.visible : "?"
        }`,
      });
    }
    if (this.plugin._cfLastRelative) {
      status.createEl("p", {
        text: `Last sync: ${this.plugin._cfLastRelative}`,
      });
    }

    new Setting(containerEl)
      .setName("Cursor 默认项目文件夹")
      .setDesc(
        "Cursor Sidecar 打开 / Attach 时固定使用此文件夹作为 Cursor 项目。默认是当前 Obsidian 库根目录。"
      )
      .addText((text) => {
        text
          .setPlaceholder("例如 D:\\胡梦浩obsidian")
          .setValue(this.plugin.settings.cursorProjectFolder || "")
          .onChange(async (value) => {
            this.plugin.settings.cursorProjectFolder = value.trim();
            await this.plugin.saveSettings();
          });
        text.inputEl.style.width = "100%";
      })
      .addButton((btn) =>
        btn.setButtonText("填入当前库").onClick(async () => {
          const vault = this.plugin.vaultPath();
          this.plugin.settings.cursorProjectFolder = vault;
          await this.plugin.saveSettings();
          this.display();
        })
      );

    new Setting(containerEl)
      .setName("Always open this vault in Cursor on Attach")
      .setDesc(
        "On Attach, open the Cursor 默认项目文件夹 in the bound Cursor Editor (--reuse-window)."
      )
      .addToggle((toggle) =>
        toggle.setValue(this.plugin.settings.openVaultInCursor).onChange(async (value) => {
          this.plugin.settings.openVaultInCursor = value;
          await this.plugin.saveSettings();
        })
      );

    new Setting(containerEl)
      .setName("Show notices")
      .addToggle((toggle) =>
        toggle.setValue(this.plugin.settings.showNotices).onChange(async (value) => {
          this.plugin.settings.showNotices = value;
          await this.plugin.saveSettings();
        })
      );

    containerEl.createEl("h3", { text: "Daemon" });
    new Setting(containerEl)
      .setName("Daemon host")
      .setDesc("localhost only")
      .addText((text) =>
        text
          .setPlaceholder("127.0.0.1")
          .setValue(this.plugin.settings.daemonHost)
          .onChange(async (value) => {
            this.plugin.settings.daemonHost = value.trim() || "127.0.0.1";
            await this.plugin.saveSettings();
          })
      );

    new Setting(containerEl)
      .setName("Daemon port")
      .addText((text) =>
        text
          .setPlaceholder("27845")
          .setValue(String(this.plugin.settings.daemonPort || 27845))
          .onChange(async (value) => {
            const n = Number(value);
            this.plugin.settings.daemonPort = Number.isFinite(n) && n > 0 ? n : 27845;
            await this.plugin.saveSettings();
          })
      );

    new Setting(containerEl)
      .setName("Cursor executable path")
      .setDesc("Optional. Custom Cursor.exe (e.g. E:\\cursor\\Cursor.exe). Auto-discovered when possible.")
      .addText((text) =>
        text
          .setPlaceholder("E:\\cursor\\Cursor.exe")
          .setValue(this.plugin.settings.cursorExePath || "")
          .onChange(async (value) => {
            this.plugin.settings.cursorExePath = value.trim();
            await this.plugin.saveSettings();
            try {
              await this.plugin.ensureRuntimeInfo();
              if (await this.plugin.daemonHealthy()) {
                await this.plugin.httpJson("POST", "/rpc", {
                  cmd: "set-cursor-exe",
                  path: this.plugin.settings.cursorExePath,
                });
              }
            } catch (_e) {
              /* ignore */
            }
          })
      );

    new Setting(containerEl)
      .setName("Developer mode")
      .setDesc("Show source-directory / Python settings for local development.")
      .addToggle((toggle) =>
        toggle.setValue(!!this.plugin.settings.developerMode).onChange(async (value) => {
          this.plugin.settings.developerMode = !!value;
          await this.plugin.saveSettings();
          this.display();
        })
      );

    if (this.plugin.settings.developerMode) {
      containerEl.createEl("h3", { text: "Advanced / Developer" });
      new Setting(containerEl)
        .setName("Sidecar source directory")
        .setDesc("Folder with main.py (developer fallback when packaged exe is absent)")
        .addText((text) =>
          text
            .setPlaceholder("D:\\…\\cursor-sidecar")
            .setValue(this.plugin.settings.sidecarDir)
            .onChange(async (value) => {
              this.plugin.settings.sidecarDir = value.trim();
              await this.plugin.saveSettings();
            })
        );

      new Setting(containerEl)
        .setName("Python path")
        .setDesc("Optional. Defaults to sidecar .venv\\Scripts\\python.exe")
        .addText((text) =>
          text
            .setPlaceholder("…\\.venv\\Scripts\\python.exe")
            .setValue(this.plugin.settings.pythonPath)
            .onChange(async (value) => {
              this.plugin.settings.pythonPath = value.trim();
              await this.plugin.saveSettings();
            })
        );

      new Setting(containerEl)
        .setName("Data directory override")
        .setDesc("Optional. Same as CURSOR_SIDECAR_DATA_DIR / --data-dir")
        .addText((text) =>
          text
            .setPlaceholder("%LOCALAPPDATA%\\CursorSidecar")
            .setValue(this.plugin.settings.dataDirOverride || "")
            .onChange(async (value) => {
              this.plugin.settings.dataDirOverride = value.trim();
              await this.plugin.saveSettings();
            })
        );
    }

    new Setting(containerEl)
      .setName("Actions")
      .addButton((btn) =>
        btn.setButtonText("Attach").onClick(async () => this.plugin.runAction("attach"))
      )
      .addButton((btn) =>
        btn.setButtonText("Detach").onClick(async () => this.plugin.runAction("detach"))
      )
      .addButton((btn) =>
        btn.setButtonText("Bind Agents").onClick(async () => this.plugin.bindAgentsWindowFlow())
      )
      .addButton((btn) =>
        btn.setButtonText("Open Native Pane").onClick(async () => this.plugin.openNativeAgentsPane())
      )
      .addButton((btn) =>
        btn.setButtonText("Recover Native").onClick(async () => this.plugin.recoverNativeAgents())
      )
      .addButton((btn) =>
        btn.setButtonText("Status").onClick(async () => this.plugin.runAction("status", true))
      );
    if (this.plugin.settings.developerMode) {
      new Setting(containerEl)
        .setName("Debug")
        .addButton((btn) =>
          btn.setButtonText("Bind Agents (debug)").onClick(async () => this.plugin.bindAgentsWindowFlow())
        );
    }
  }
}

module.exports = CursorSidecarPlugin;
