const { Plugin, Notice, PluginSettingTab, Setting, addIcon, MarkdownView, TFile, FileSystemAdapter } = require("obsidian");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const http = require("http");

const ICON_ID = "cursor-sidecar";
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
  showNotices: true,
};

const CONTEXT_FOLLOW_DEBOUNCE_MS = 150;
const CONTEXT_FOLLOW_SUPPRESS_MS = 400;

class CursorSidecarPlugin extends Plugin {
  async onload() {
    const saved = await this.loadData();
    this.settings = Object.assign({}, DEFAULT_SETTINGS, saved || {});
    // Migrate v0.2.1 useDaemon → liveSidecar
    if (saved && saved.liveSidecar === undefined && saved.useDaemon !== undefined) {
      this.settings.liveSidecar = !!saved.useDaemon;
    }
    addIcon(ICON_ID, ICON_SVG);

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
  }

  async saveSettings() {
    await this.saveData(this.settings);
  }

  notify(message, isError = false) {
    if (!this.settings.showNotices && !isError) return;
    new Notice(message, isError ? 8000 : 4000);
  }

  vaultPath() {
    const adapter = this.app.vault.adapter;
    if (adapter && typeof adapter.getBasePath === "function") {
      return adapter.getBasePath();
    }
    return adapter && adapter.basePath ? adapter.basePath : "";
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
    const vaultRoot = this.vaultPath();
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

      this._cfSeq += 1;
      const body = {
        cmd: "sync-editor-file",
        vault_root: vaultRoot,
        path: absPath,
        relative_path: file.path,
        seq: this._cfSeq,
      };
      if (line !== null) body.line = line;
      if (column !== null) body.column = column;

      const result = await this.httpJson("POST", "/rpc", body);
      // Success: no Notice (silent follow)
      if (!result || result.ok === false) {
        const err = (result && result.error) || "sync failed";
        if (err === "sidecar_not_attached" || err === "stale_binding") {
          this.notifyContextFollowError(err, `Cursor Sidecar: ${err}`);
        }
        return null;
      }
      this._cfLastPath = absPath;
      this._cfLastLine = line;
      this._cfLastSyncAt = Date.now();
      this._cfLastRelative = file.path;
      return result;
    } catch (_e) {
      return null;
    }
  }

  helperFrontArgs() {
    const dataDir = this.resolveDataDir();
    const args = [];
    if (dataDir) {
      args.push("--data-dir", dataDir);
    }
    return args;
  }

  async ensureDaemon() {
    if (await this.daemonHealthy()) return true;
    if (!this.helperConfigured()) return false;
    const host = this.settings.daemonHost || "127.0.0.1";
    const port = String(Number(this.settings.daemonPort) || 27845);
    await this.runHelper(["daemon-start", "--host", host, "--port", port]);
    for (let i = 0; i < 15; i++) {
      await sleep(250);
      if (await this.daemonHealthy()) return true;
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
      body.workspace = this.vaultPath();
    }
    return body;
  }

  async runAction(cmd, showRaw = false) {
    if (!this.helperConfigured()) return null;
    this.notify(`Cursor Sidecar: ${cmd}…`);
    try {
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
            this.notify("Cursor Sidecar: detached (windows restored)");
          } else {
            this.notify(`Cursor Sidecar: ${cmd} ok`);
          }
          return result;
        }
        this.notify("Cursor Sidecar: daemon unavailable — falling back to one-shot CLI", true);
      }
      return await this.runHelperCommand(cmd, showRaw);
    } catch (err) {
      this.notify(`Cursor Sidecar error: ${err.message || err}`, true);
      return null;
    }
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
      extra.push("--workspace", this.vaultPath());
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
      text: `Version: ${(this.plugin.manifest && this.plugin.manifest.version) || "0.6.0"}`,
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

    status.createEl("p", {
      text: `Context Follow: ${this.plugin.settings.contextFollow ? "On" : "Off"}`,
    });
    if (this.plugin._cfLastRelative) {
      status.createEl("p", {
        text: `Last sync: ${this.plugin._cfLastRelative}`,
      });
    }

    new Setting(containerEl)
      .setName("Open vault in Cursor on Attach")
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
        btn.setButtonText("Status").onClick(async () => this.plugin.runAction("status", true))
      );
  }
}

module.exports = CursorSidecarPlugin;
