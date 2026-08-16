/**
 * The one settings tab of the plugin (spec 19): exact server origin, editable
 * device name, closed connection status, Login, Open browser again, Cancel
 * pending login and Disconnect. The tab renders from a snapshot and delegates
 * every action to the injected view; it holds no state of its own.
 */

import { PluginSettingTab, Setting } from "obsidian";
import type { App, Plugin } from "obsidian";

import { CONNECTION_STATUS_TEXT, resolveAuthenticationControls } from "./contracts";
import type { ConnectionState } from "./contracts";

export interface DeviceAuthenticationSnapshot {
  readonly connectionState: ConnectionState;
  readonly statusDetail: string | null;
  readonly serverOrigin: string;
  readonly deviceName: string;
  readonly hasPendingGrant: boolean;
  readonly hasActiveCredential: boolean;
}

export interface DeviceAuthenticationTabView {
  getSnapshot(): DeviceAuthenticationSnapshot;
  setServerOrigin(origin: string): void;
  setDeviceName(name: string): void;
  login(): Promise<void>;
  openBrowserAgain(): void;
  cancelPendingLogin(): Promise<void>;
  disconnect(): Promise<void>;
}

export class DeviceAuthenticationSettingTab extends PluginSettingTab {
  readonly #view: DeviceAuthenticationTabView;

  constructor(app: App, plugin: Plugin, view: DeviceAuthenticationTabView) {
    super(app, plugin);
    this.#view = view;
  }

  override display(): void {
    const containerEl = this.containerEl;
    containerEl.empty();

    const snapshot = this.#view.getSnapshot();
    const controls = resolveAuthenticationControls(snapshot.connectionState, {
      hasPendingGrant: snapshot.hasPendingGrant,
      hasActiveCredential: snapshot.hasActiveCredential,
    });

    const statusText = CONNECTION_STATUS_TEXT[snapshot.connectionState];
    const statusDescription =
      snapshot.statusDetail === null ? statusText : `${statusText} — ${snapshot.statusDetail}`;

    new Setting(containerEl)
      .setName("Connection status")
      .setDesc(statusDescription);

    new Setting(containerEl)
      .setName("Server origin")
      .setDesc("Exact HTTPS origin of the personal knowledge API")
      .addText((text) =>
        text
          .setPlaceholder("https://vault.example.com")
          .setValue(snapshot.serverOrigin)
          .onChange((value) => this.#view.setServerOrigin(value.trim())),
      );

    new Setting(containerEl)
      .setName("Device name")
      .setDesc("1–80 display characters shown on the approval page")
      .addText((text) =>
        text
          .setPlaceholder("Personal vault")
          .setValue(snapshot.deviceName)
          .onChange((value) => this.#view.setDeviceName(value)),
      );

    const actionSetting = new Setting(containerEl);
    actionSetting.addButton((button) =>
      button
        .setButtonText("Login")
        .setDisabled(!controls.canLogin)
        .onClick(() => {
          void this.#runAction(this.#view.login());
        }),
    );
    actionSetting.addButton((button) =>
      button
        .setButtonText("Open browser again")
        .setDisabled(!controls.canOpenBrowser)
        .onClick(() => {
          this.#view.openBrowserAgain();
          this.display();
        }),
    );
    actionSetting.addButton((button) =>
      button
        .setButtonText("Cancel pending login")
        .setDisabled(!controls.canCancel)
        .onClick(() => {
          void this.#runAction(this.#view.cancelPendingLogin());
        }),
    );
    actionSetting.addButton((button) =>
      button
        .setButtonText("Disconnect")
        .setDisabled(!controls.canDisconnect)
        .onClick(() => {
          void this.#runAction(this.#view.disconnect());
        }),
    );
  }

  #runAction(action: Promise<void>): void {
    action.then(
      () => this.display(),
      () => this.display(),
    );
  }
}
