const { Plugin, Notice, PluginSettingTab, Setting, addIcon } = require("obsidian");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const ICON_ID = "cursor-sidecar";
const ICON_SVG = `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
  <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2"/>
  <circle cx="12" cy="12" r="3.5" fill="currentColor"/>
</svg>`;

const DEFAULT_SETTINGS = {
  sidecarDir: "D:\\桌面\\cursor-obsidian\\cursor-sidecar",
  pythonPath: "D:\\桌面\\cursor-obsidian\\cursor-sidecar\\.venv\\Scripts\\python.exe",
  openVaultInCursor: true,
  showNotices: true,
};

class CursorSidecarPlugin extends Plugin {
  async onload() {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
    addIcon(ICON_ID, ICON_SVG);

    this.addRibbonIcon(ICON_ID, "Cursor Sidecar", async () => {
      await this.runSidecar("click");
    });

    this.addCommand({
      id: "cursor-sidecar-click",
      name: "Toggle / dock Cursor",
      callback: async () => this.runSidecar("click"),
    });
    this.addCommand({
      id: "cursor-sidecar-dock",
      name: "Dock Cursor (launch + arrange)",
      callback: async () => this.runSidecar("dock"),
    });
    this.addCommand({
      id: "cursor-sidecar-arrange",
      name: "Arrange windows",
      callback: async () => this.runSidecar("arrange"),
    });
    this.addCommand({
      id: "cursor-sidecar-toggle",
      name: "Show / hide Cursor",
      callback: async () => this.runSidecar("toggle"),
    });
    this.addCommand({
      id: "cursor-sidecar-status",
      name: "Sidecar status",
      callback: async () => this.runSidecar("status", ["--json"], true),
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
    const venvPy = path.join(this.settings.sidecarDir, ".venv", "Scripts", "python.exe");
    if (fs.existsSync(venvPy)) return venvPy;
    return "python";
  }

  resolveMainPy() {
    return path.join(this.settings.sidecarDir, "main.py");
  }

  async runSidecar(command, extraArgs = [], returnStdout = false) {
    const mainPy = this.resolveMainPy();
    if (!fs.existsSync(mainPy)) {
      this.notify(
        `Cursor Sidecar: main.py not found.\nSet Sidecar directory in settings.\nExpected: ${mainPy}`,
        true
      );
      return null;
    }

    const python = this.resolvePython();
    const finalArgs = [mainPy];
    if (
      this.settings.openVaultInCursor &&
      (command === "click" || command === "dock" || command === "start")
    ) {
      finalArgs.push("--workspace", this.app.vault.adapter.basePath);
    }
    finalArgs.push(command, ...extraArgs);

    this.notify(`Cursor Sidecar: ${command}…`);

    try {
      const result = await this.spawnAsync(python, finalArgs, this.settings.sidecarDir);
      if (result.code !== 0) {
        const errText = (result.stderr || result.stdout || `exit ${result.code}`).trim();
        this.notify(`Cursor Sidecar failed: ${errText.slice(0, 300)}`, true);
        return null;
      }
      if (returnStdout) {
        this.notify(`Cursor Sidecar: ${(result.stdout || "").trim().slice(0, 280)}`);
        return result.stdout;
      }
      const out = (result.stdout || "").trim();
      if (out) {
        const last = out.split(/\r?\n/).filter(Boolean).pop();
        this.notify(last || `Cursor Sidecar: ${command} ok`);
      } else {
        this.notify(`Cursor Sidecar: ${command} ok`);
      }
      return result.stdout;
    } catch (err) {
      this.notify(`Cursor Sidecar error: ${err.message || err}`, true);
      return null;
    }
  }

  spawnAsync(exe, args, cwd) {
    return new Promise((resolve, reject) => {
      const child = spawn(exe, args, {
        cwd,
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
      text: "Calls the local Python helper to dock real Cursor Desktop beside Obsidian. No embedding, no Cursor injection.",
    });

    new Setting(containerEl)
      .setName("Sidecar directory")
      .setDesc("Folder containing main.py / config.json / .venv")
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
      .setDesc("Prefer the sidecar .venv python.exe")
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
      .setName("Open vault in Cursor")
      .setDesc("Pass current vault path when docking / clicking")
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
      .setName("Test arrange")
      .setDesc("Run python main.py arrange now")
      .addButton((btn) =>
        btn.setButtonText("Arrange").onClick(async () => {
          await this.plugin.runSidecar("arrange");
        })
      );
  }
}

module.exports = CursorSidecarPlugin;
