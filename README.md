# Deskflow Autostart

A [decky-loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that starts
[Deskflow](https://github.com/deskflow/deskflow) automatically when Steam boots, and lets you
configure the connection from the Decky panel.

## Features

- **Start on boot** — Deskflow launches when Steam starts (Gaming Mode), shortly after the
  gamescope display becomes usable. The plugin needs no systemd changes and runs entirely as
  your user.
- **Client or server mode** — connect the Deck out to a Deskflow server, or let the Deck accept
  incoming clients.
- **Server address**, **named config** selection and **extra arguments**, all editable from the
  panel and persisted immediately.
- **Start / Stop / Restart** buttons with live status, and a log of the launched process.

## How the launch method is chosen

The plugin prefers the dedicated headless binaries and falls back to the GUI:

| Mode   | Preference order                                                     |
| ------ | -------------------------------------------------------------------- |
| Client | `deskflow-client` → `deskflow` (GUI) → Flatpak `org.deskflow.deskflow` |
| Server | `deskflow-server` → `deskflow` (GUI, with `--host`) → Flatpak         |

Binaries are looked up in `PATH` and then in `~/.local/bin`, `/usr/local/bin` and `/usr/bin`.
The panel shows which method was detected.

A Flatpak is only considered if no binary is present, and is launched with
`flatpak run org.deskflow.deskflow`.

## Settings

| Setting        | Default  | Notes                                                                     |
| -------------- | -------- | ------------------------------------------------------------------------- |
| `autostart`    | `true`   | Launch Deskflow when Steam starts.                                        |
| `role`         | `client` | `client` connects out; `server` listens for clients.                     |
| `server`       | `""`     | Client: the host/IP of the server. Server: local interface to listen on. |
| `config`       | `""`     | Named config from `~/.config/deskflow/*.conf`, extension stripped.        |
| `extra_args`   | `""`     | Appended to the command line, e.g. `--debug INFO --enable-crypto`.        |

Stored in `settings.json` under the plugin's settings directory. The launched process writes to
`deskflow.log` in the plugin's runtime directory.

### A note on `--address`

This is easy to get wrong, and the plugin models it explicitly:

- `deskflow-client --address <host>` connects to a remote **server**.
- `deskflow-server --address <host>` sets the local interface to **listen** on (it does not
  connect anywhere). A server also needs a config that links your clients, so in server mode you
  normally want a named config.

`--no-daemon` is added for the headless binaries so the plugin owns the process directly. It is
not passed to the GUI, which does not daemonise.

## Safety behaviour

The plugin is deliberately conservative about process management, because a wrong signal on a
Steam Deck can end the session:

- Processes are identified by **executable name**, never by matching "deskflow" anywhere in a
  command line — otherwise the plugin's own paths and shortcuts would match.
- A process is only ever signalled with a **single-pid** signal, never a process-group signal,
  unless the plugin itself spawned it with `start_new_session=True` and has confirmed it is a
  group leader. A GUI app started from Steam is *not* a group leader; killing its group would
  kill Steam itself.
- Its own pid, pid 1, its process group and its entire ancestor chain are never signalled.
- `Stop`/unload/uninstall only terminate Deskflow that this plugin started. A Deskflow you
  launched yourself is left running.

## Troubleshooting

**"Deskflow not detected"** — install it, then press *Refresh status*. The panel shows the
detected launch method once found.

**Status says Stopped but Deskflow is running** — a process sharing the plugin's own process
group is deliberately ignored, to avoid signalling the session. Restart Steam and check again.

**It starts and immediately exits** — the plugin captures the last few log lines and shows them
in the panel. Check `deskflow.log` in the plugin's runtime directory for the full output. Common
causes: a server-mode launch with no config, or a server address that is not reachable.

**Autostart does not fire** — it runs from the plugin's `_main`, so it only happens once Steam
starts, not at machine power-on. Check the plugin log for `Autostart did not launch Deskflow`.

**A GUI or Flatpak Deskflow needs a Steam shortcut** — for input to work reliably under
gamescope, launching the Flatpak as a non-Steam game is often more reliable than letting the
plugin launch it. In that case leave *Start on boot* off and start it from your library.

## Development

```bash
pnpm i
pnpm run build
```

Backend changes are in `main.py`; the frontend is `src/index.tsx`. Rebuild the frontend after
changing it.

## License

BSD-3-Clause. See `LICENSE`.
