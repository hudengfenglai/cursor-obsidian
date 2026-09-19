const { Plugin, Notice, PluginSettingTab, Setting, addIcon } = require("obsidian");
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
  daemonHost: "127.0.0.1",
  daemonPort: 27845,
  useDaemon: false,
  openVaultInCursor: true,
  showNotices: true,
};

class CursorSidecarPlugin extends Plugin {
  async onload() {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
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

    this.addSettingTab(new CursorSidecarSettingTab(this.app, this));
  }

  onunload() {}

  async saveSettings() {
    await this.saveData(this.settings);
  }

  notify(message, isError = false) {
    if (!this.settings.showNotices && !isError) return;
    new Notice(message, isError ? 8000 : 4000);
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

  resolveMainPy() {
    return path.join((this.settings.sidecarDir || "").trim(), "main.py");
  }

  pathsConfigured() {
    const dir = (this.settings.sidecarDir || "").trim();
    if (!dir) {
      this.notify(
        "Cursor Sidecar: set Sidecar directory in Settings → Cursor Sidecar",
        true
      );
      return false;
    }
    if (!fs.existsSync(this.resolveMainPy())) {
      this.notify(`Cursor Sidecar: main.py not found in ${dir}`, true);
      return false;
    }
    return true;
  }

  vaultPath() {
    return this.app.vault.adapter.basePath;
  }

  resolveDaemonToken() {
    const dir = (this.settings.sidecarDir || "").trim();
    if (!dir) return "";
    const tokenPath = path.join(dir, ".sidecar.daemon.token");
    try {
      if (fs.existsSync(tokenPath)) {
        return fs.readFileSync(tokenPath, "utf8").trim();
      }
    } catch (_e) {
      /* ignore */
    }
    return "";
  }

  async ensureDaemon() {
    if (await this.daemonHealthy()) return true;
    if (!this.pathsConfigured()) return false;
    await this.spawnAsync(
      this.resolvePython(),
      [this.resolveMainPy(), "daemon-start"],
      this.settings.sidecarDir
    );
    for (let i = 0; i < 10; i++) {
      await sleep(200);
      if (await this.daemonHealthy()) return true;
    }
    return false;
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
    if (!this.pathsConfigured()) return null;
    this.notify(`Cursor Sidecar: ${cmd}…`);
    try {
      if (this.settings.useDaemon) {
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
          } else if (result.attached === true) {
            this.notify("Cursor Sidecar: attached");
          } else if (result.attached === false) {
            this.notify("Cursor Sidecar: detached (windows restored)");
          } else {
            this.notify(`Cursor Sidecar: ${cmd} ok`);
          }
          return result;
        }
      }
      return await this.runCli(cmd, showRaw);
    } catch (err) {
      this.notify(`Cursor Sidecar error: ${err.message || err}`, true);
      return null;
    }
  }

  async runCli(command, returnStdout = false, extraFrontArgs = []) {
    const mainPy = this.resolveMainPy();
    const finalArgs = [mainPy, ...extraFrontArgs];
    if (
      this.settings.openVaultInCursor &&
      (command === "click" || command === "attach" || command === "dock") &&
      !extraFrontArgs.includes("--workspace")
    ) {
      finalArgs.push("--workspace", this.vaultPath());
    }
    if (command === "status") {
      finalArgs.push("status", "--json");
    } else {
      finalArgs.push(command);
    }

    const result = await this.spawnAsync(
      this.resolvePython(),
      finalArgs,
      this.settings.sidecarDir
    );
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
      text: "Attach / Detach real Cursor Desktop beside Obsidian. Set Sidecar directory to the folder that contains main.py.",
    });

    new Setting(containerEl)
      .setName("Sidecar directory")
      .setDesc("Required. Folder with main.py / config.json / .venv")
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
      .setName("Use daemon (experimental)")
      .setDesc("Off by default. Requires .sidecar.daemon.token; no browser CORS.")
      .addToggle((toggle) =>
        toggle.setValue(this.plugin.settings.useDaemon).onChange(async (value) => {
          this.plugin.settings.useDaemon = value;
          await this.plugin.saveSettings();
        })
      );

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
