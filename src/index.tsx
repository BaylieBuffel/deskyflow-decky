import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  staticClasses,
  TextField,
  ToggleField
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useCallback, useEffect, useRef, useState } from "react";
import { FaPlay, FaProjectDiagram, FaStop, FaSyncAlt } from "react-icons/fa";

type Role = "client" | "server";

interface Settings {
  autostart: boolean;
  role: Role;
  server: string;
  config: string;
  extra_args: string;
}

interface Launcher {
  available: boolean;
  label: string | null;
}

interface Status {
  running: boolean;
  managed: boolean;
  pids: number[];
  processes: string[];
  role: Role;
  autostart: boolean;
  launchers: { client: Launcher; server: Launcher };
  config_dir: string;
  last_error: string | null;
}

const DEFAULTS: Settings = {
  autostart: true,
  role: "client",
  server: "",
  config: "",
  extra_args: ""
};

const getSettings = callable<[], Settings>("get_settings");
const setSettings = callable<[Settings], Settings>("set_settings");
const getStatus = callable<[], Status>("get_status");
const getConfigs = callable<[], string[]>("get_configs");
const startDeskflow = callable<[], { started: boolean; error?: string }>("start_deskflow");
const stopDeskflow = callable<[], { stopped: boolean }>("stop_deskflow");
const restartDeskflow = callable<[], { started: boolean; error?: string }>("restart_deskflow");

const ROLE_OPTIONS = [
  { label: "Client - this Deck connects out", data: "client" },
  { label: "Server - this Deck accepts clients", data: "server" }
];

function Content() {
  const [settings, setLocalSettings] = useState<Settings>(DEFAULTS);
  const [status, setStatus] = useState<Status | null>(null);
  const [configs, setConfigs] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  // The current settings, readable from callbacks that must not close over a
  // stale render. Two saves in the same tick (toggling autostart, then changing
  // mode) would otherwise each send the pre-tick object and revert the first.
  const settingsRef = useRef<Settings>(DEFAULTS);

  const applySettings = useCallback((next: Settings) => {
    settingsRef.current = next;
    if (mounted.current) setLocalSettings(next);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextConfigs] = await Promise.all([getStatus(), getConfigs()]);
      if (!mounted.current) return;
      setStatus(nextStatus);
      setConfigs(nextConfigs);
    } catch (err) {
      console.error("Deskflow status refresh failed", err);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    getSettings()
      .then((loaded) => {
        if (mounted.current) applySettings({ ...DEFAULTS, ...loaded });
      })
      .catch((err) => console.error("Deskflow settings load failed", err));
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, [refresh, applySettings]);

  const save = useCallback(
    async (patch: Partial<Settings>) => {
      const next = { ...settingsRef.current, ...patch };
      applySettings(next);
      try {
        const saved = await setSettings(next);
        if (mounted.current) applySettings({ ...DEFAULTS, ...saved });
      } catch (err) {
        console.error("Deskflow settings save failed", err);
        toaster.toast({ title: "Deskflow", body: `Could not save settings: ${err}` });
      }
    },
    [applySettings]
  );

  const run = useCallback(
    async (action: () => Promise<unknown>) => {
      setBusy(true);
      try {
        // Flush any edit still sitting in local state (the text fields only save
        // on blur) so a start triggered straight after typing does not launch
        // with the settings that were on disk a moment earlier. A failed flush is
        // not allowed to block the action: Stop must always be available.
        try {
          const saved = await setSettings(settingsRef.current);
          if (mounted.current) applySettings({ ...DEFAULTS, ...saved });
        } catch (err) {
          console.error("Deskflow settings flush failed", err);
        }
        const result = (await action()) as { error?: string } | undefined;
        if (result?.error) {
          toaster.toast({ title: "Deskflow", body: result.error });
        }
      } catch (err) {
        console.error("Deskflow action failed", err);
        toaster.toast({ title: "Deskflow", body: String(err) });
      } finally {
        setBusy(false);
        await refresh();
      }
    },
    [refresh, applySettings]
  );

  const launcher = status?.launchers[settings.role];
  const running = status?.running ?? false;
  const managed = status?.managed ?? false;
  const statusLabel = !status
    ? "Checking..."
    : running
      ? managed
        ? `Running (${status.pids.length} proc)`
        : "Running (started outside this plugin)"
      : "Stopped";
  const configOptions = [
    { label: "Default / none", data: "" },
    ...configs.map((name) => ({ label: name, data: name }))
  ];

  return (
    <>
      <PanelSection title="Deskflow">
        <PanelSectionRow>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span>Status</span>
            <span style={{ color: running ? "#59bf40" : "var(--decky-text-secondary)" }}>
              {statusLabel}
            </span>
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Start on boot"
            description="Launch Deskflow automatically when Steam starts"
            checked={settings.autostart}
            onChange={(autostart) => save({ autostart })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={{ display: "flex", gap: "8px", width: "100%" }}>
            <ButtonItem
              layout="below"
              disabled={busy || !managed}
              onClick={() => run(stopDeskflow)}
            >
              <FaStop /> Stop
            </ButtonItem>
            <ButtonItem
              layout="below"
              disabled={busy || !managed}
              onClick={() => run(restartDeskflow)}
            >
              <FaSyncAlt /> Restart
            </ButtonItem>
            <ButtonItem
              layout="below"
              disabled={busy || running}
              onClick={() => run(startDeskflow)}
            >
              <FaPlay /> Start
            </ButtonItem>
          </div>
        </PanelSectionRow>
        {status?.last_error && (
          <PanelSectionRow>
            <div style={{ color: "#f2545b", fontSize: "12px", whiteSpace: "normal" }}>
              {status.last_error}
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Connection">
        <PanelSectionRow>
          <Field
            label="Mode"
            description="Client connects to another machine, Server waits for clients to connect"
          >
            <DropdownItem
              rgOptions={ROLE_OPTIONS}
              selectedOption={settings.role}
              onChange={(item) => save({ role: item.data as Role })}
            />
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label="Server address"
            description={
              settings.role === "client"
                ? "Host or IP of the machine running the Deskflow server, e.g. 192.168.1.10"
                : "Optional interface to listen on, e.g. 192.168.1.20:24800"
            }
          >
            <TextField
              value={settings.server}
              onChange={(event) => setLocalSettings({ ...settings, server: event.target.value })}
              onBlur={() => save({})}
            />
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label="Named config"
            description={`Configs found in ${status?.config_dir ?? "~/.config/deskflow"}`}
          >
            <DropdownItem
              rgOptions={configOptions}
              selectedOption={settings.config}
              onChange={(item) => save({ config: String(item.data) })}
            />
          </Field>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Advanced">
        <PanelSectionRow>
          <Field
            label="Extra arguments"
            description="Appended to the launch command, e.g. --debug INFO --enable-crypto"
          >
            <TextField
              value={settings.extra_args}
              onChange={(event) => setLocalSettings({ ...settings, extra_args: event.target.value })}
              onBlur={() => save({})}
            />
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={{ fontSize: "12px", color: "var(--decky-text-secondary)", whiteSpace: "normal" }}>
            {!status
              ? "Checking for Deskflow..."
              : launcher?.available
                ? `Launch method: ${launcher.label}`
                : "Deskflow not detected. Install it via apt or as a Flatpak, then refresh."}
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => refresh()}>
            Refresh status
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => ({
  name: "Deskflow Autostart",
  titleView: <div className={staticClasses.Title}>Deskflow</div>,
  content: <Content />,
  icon: <FaProjectDiagram />,
  onDismount() {
    console.log("Deskflow Autostart unloaded");
  }
}));
