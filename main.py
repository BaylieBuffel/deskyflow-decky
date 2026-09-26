import asyncio
import json
import os
import shlex
import shutil
import signal
import subprocess
import time

import decky

FLATPAK_APP_ID = "org.deskflow.deskflow"
GUI_BINARY = "deskflow"

# Executables that are genuinely Deskflow. Used for process identification, so
# anything not listed here is never signalled.
DESKFLOW_EXECUTABLES = frozenset(
    {"deskflow", "deskflow-client", "deskflow-server", "deskflow-core"}
)
# Wrappers a Flatpak launch may sit behind.
FLATPAK_LAUNCHERS = frozenset(
    {"flatpak", "bwrap", "dbus-run-session", "xdg-dbus-proxy", "flatpak-session-helper"}
)
# Interpreters, used so a Deskflow shipped as a wrapper script is still
# identified by the script name in argv[1].
INTERPRETERS = frozenset({"sh", "bash", "dash", "zsh", "ash", "env", "python", "python3"})

# Ordered by preference. The dedicated headless binaries are preferred over the
# GUI so that auto start behaves predictably inside gamescope.
CLIENT_BINARIES = ("deskflow-client", GUI_BINARY)
SERVER_BINARIES = ("deskflow-server", GUI_BINARY)

# The Steam client/Decky need a moment before the gamescope display is usable.
AUTOSTART_DELAY = 8
STOP_GRACE_PERIOD = 5
# get_status() is polled by the panel, so the flatpak probe is cached.
FLATPAK_CACHE_TTL = 60

DEFAULT_SETTINGS = {
    "autostart": True,
    "role": "client",
    "server": "",
    "config": "",
    "extra_args": "",
}


class DeskflowNotFound(Exception):
    pass


def _settings_dir() -> str:
    return getattr(decky, "DECKY_PLUGIN_SETTINGS_DIR", None) or os.path.join(
        decky.DECKY_HOME, "settings", "DeskflowAutostart"
    )


def _runtime_dir() -> str:
    return getattr(decky, "DECKY_PLUGIN_RUNTIME_DIR", None) or os.path.join(
        decky.DECKY_HOME, "data", "DeskflowAutostart"
    )


def _settings_path() -> str:
    return os.path.join(_settings_dir(), "settings.json")


def _state_path() -> str:
    return os.path.join(_runtime_dir(), "state.json")


def _child_log_path() -> str:
    return os.path.join(_runtime_dir(), "deskflow.log")


def _user_config_dir() -> str:
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        decky.DECKY_USER_HOME, ".config"
    )
    return os.path.join(config_home, "deskflow")


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(tmp_path, path)


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        decky.logger.warning("Could not read %s: %s", path, err)
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalise(settings: dict) -> dict:
    clean = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        if key in settings:
            clean[key] = settings[key]
    if clean["role"] not in ("client", "server"):
        clean["role"] = DEFAULT_SETTINGS["role"]
    clean["autostart"] = bool(clean["autostart"])
    for key in ("server", "config", "extra_args"):
        if clean[key] is None:
            clean[key] = ""
        clean[key] = str(clean[key]).strip()
    return clean


def _find_binary(name: str):
    found = shutil.which(name)
    if found:
        return found
    for base in (os.path.join(decky.DECKY_USER_HOME, ".local", "bin"), "/usr/local/bin", "/usr/bin"):
        candidate = os.path.join(base, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


_flatpak_cache = {"checked_at": 0.0, "installed": False}


def _flatpak_installed() -> bool:
    """Cached, because the panel polls get_status() and flatpak list is slow."""
    now = time.monotonic()
    if now - _flatpak_cache["checked_at"] < FLATPAK_CACHE_TTL:
        return _flatpak_cache["installed"]
    if not shutil.which("flatpak"):
        _flatpak_cache.update({"checked_at": now, "installed": False})
        return False
    try:
        result = subprocess.run(
            ["flatpak", "list", "--app", "--columns=application"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        installed = FLATPAK_APP_ID in result.stdout.split()
    except (OSError, subprocess.SubprocessError) as err:
        decky.logger.debug("flatpak list failed: %s", err)
        installed = False
    _flatpak_cache.update({"checked_at": now, "installed": installed})
    return installed


def _launcher_for(role: str):
    """Resolve how Deskflow should be launched for the given role.

    Returns None when no Deskflow installation could be found.
    """
    names = SERVER_BINARIES if role == "server" else CLIENT_BINARIES
    for name in names:
        path = _find_binary(name)
        if path:
            return {
                "kind": "binary",
                "headless": name != GUI_BINARY,
                "as_host": name == GUI_BINARY,
                "label": name,
                "argv": [path],
            }
    if _flatpak_installed():
        return {
            "kind": "flatpak",
            "headless": False,
            "as_host": True,
            "label": f"Flatpak ({FLATPAK_APP_ID})",
            "argv": ["flatpak", "run", FLATPAK_APP_ID],
        }
    return None


def _describe_launcher(role: str):
    launcher = _launcher_for(role)
    if launcher is None:
        return {"available": False, "label": None}
    return {"available": True, "label": launcher["label"]}


def _resolve_config(name: str) -> str:
    """Expand a named config into a path the child can actually open.

    get_configs() lists names with the extension stripped, and the child runs with
    cwd set to the home directory, so a bare name does not resolve to anything from
    there. Anything that already looks like a path is passed through untouched.
    """
    if not name or os.path.isabs(name) or os.path.sep in name:
        return name
    directory = _user_config_dir()
    for candidate in (f"{name}.conf", name):
        path = os.path.join(directory, candidate)
        if os.path.isfile(path):
            return path
    return name


def _build_argv(settings: dict):
    launcher = _launcher_for(settings["role"])
    if launcher is None:
        raise DeskflowNotFound(
            "No Deskflow installation found. Install deskflow or the "
            f"{FLATPAK_APP_ID} Flatpak, then check the plugin again."
        )

    argv = list(launcher["argv"])
    # The GUI needs to be told to act as a server; the headless binary already is one.
    if settings["role"] == "server" and launcher["as_host"]:
        argv.append("--host")
    if settings["server"]:
        argv += ["--address", settings["server"]]
    if settings["config"]:
        argv += ["--config", _resolve_config(settings["config"])]
    if launcher["headless"]:
        # Stay in the foreground so the plugin owns the process directly.
        argv.append("--no-daemon")
    if settings["extra_args"]:
        try:
            argv += shlex.split(settings["extra_args"])
        except ValueError as err:
            decky.logger.warning("Ignoring unparsable extra args: %s", err)
    return argv, launcher


def _read_cmdline(pid: int) -> list:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return []
    return [part for part in raw.split("\0") if part]


def _executable_names(argv: list) -> list:
    names = [os.path.basename(argv[0])]
    if names[0] in INTERPRETERS and len(argv) > 1:
        names.append(os.path.basename(argv[1]))
    return names


def _is_deskflow_process(argv: list) -> bool:
    """Match on the executable itself, never on a substring of the whole command.

    A path that merely contains "deskflow" (this plugin's own directory, a log
    file, a Steam shortcut name) must never be treated as the Deskflow server,
    or stopping it would signal an unrelated process.
    """
    if not argv:
        return False
    names = _executable_names(argv)
    if any(name in DESKFLOW_EXECUTABLES for name in names):
        return True
    # Flatpak launches are wrapped: flatpak run <app-id>, or bwrap/dbus helpers.
    if names[0] in FLATPAK_LAUNCHERS and any(FLATPAK_APP_ID in arg for arg in argv):
        return True
    return False


def _iter_deskflow_processes():
    """Yield (pid, cmdline) for running Deskflow processes owned by this user."""
    own_pid = os.getpid()
    own_pgid = os.getpgrp()
    own_uid = os.getuid()
    protected = _protected_pids()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == own_pid or pid in protected:
            continue
        # /proc lists every process on the machine. A Deskflow owned by another
        # user (a system-wide instance, or root) is neither ours to report on nor
        # ours to signal, and including it would make the panel claim Deskflow is
        # running while this plugin has nothing it can start or stop.
        try:
            if os.stat(f"/proc/{pid}").st_uid != own_uid:
                continue
        except OSError:
            continue
        argv = _read_cmdline(pid)
        if not _is_deskflow_process(argv):
            continue
        # Never report a process that shares our group: signalling it is fine,
        # but it is far more likely to be part of the Decky/Steam session.
        try:
            if os.getpgid(pid) == own_pgid:
                continue
        except (ProcessLookupError, OSError):
            continue
        yield pid, " ".join(argv)


def _parent_pid(pid: int) -> int:
    fields = _proc_stat_fields(pid)
    if len(fields) < 2:
        return 0
    try:
        return int(fields[1])
    except ValueError:
        return 0


def _protected_pids() -> set:
    """Pids this plugin must never signal: itself, its ancestors, and pid 1.

    The ancestors of the plugin are the Decky/Steam processes that keep the
    session alive, so an accidental signal to one of them logs the user out. The
    walk continues to the root even when a pid is already in the set, because
    the process group leader is itself an ancestor and stopping there would
    leave the rest of the chain unprotected.
    """
    protected = {1, os.getpid(), os.getpgrp()}
    seen = set()
    pid = os.getpid()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        parent = _parent_pid(pid)
        if parent <= 0:
            break
        protected.add(parent)
        pid = parent
    return protected


def _proc_stat_fields(pid: int) -> list:
    """The fields of /proc/<pid>/stat that follow the (comm) field.

    comm can contain spaces and parentheses, so the fields after it are located
    from the last ")" rather than by splitting the whole line.
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as handle:
            stat = handle.read()
    except (OSError, ValueError):
        return []
    try:
        return stat[stat.rindex(")") + 1:].split()
    except ValueError:
        return []


def _proc_state(pid: int) -> str:
    fields = _proc_stat_fields(pid)
    return fields[0] if fields else ""


def _proc_starttime(pid: int) -> int:
    """Kernel start time of a pid, in clock ticks since boot.

    A pid on its own is not a stable handle: the kernel recycles pids, so a pid
    read back from a state file can belong to an unrelated process by the time it
    is used. Paired with the pid this identifies one specific process.
    """
    fields = _proc_stat_fields(pid)
    # stat field 22 overall, which is index 19 once comm is stripped.
    if len(fields) < 20:
        return 0
    try:
        return int(fields[19])
    except ValueError:
        return 0


def _reap(pid: int) -> None:
    """Clear a finished child of ours so it does not linger as a zombie."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal 0, but it has already exited: treating it as
    # alive makes every stop wait out the full grace period and then report the
    # process as unkillable.
    if _proc_state(pid) == "Z":
        _reap(pid)
        return False
    return True


def _recorded_pid(state: dict):
    """The pid from a state file, but only if it is still the process we started.

    Returns None when the recorded pid is gone, protected, or no longer the same
    process, so a recycled pid is never mistaken for the plugin's child.
    """
    pid = state.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return None
    if not _alive(pid):
        return None
    expected_start = state.get("starttime")
    if isinstance(expected_start, int) and expected_start > 0:
        if _proc_starttime(pid) != expected_start:
            decky.logger.warning(
                "Recorded pid %s now belongs to a different process, ignoring it", pid
            )
            return None
        return pid
    # State written before starttimes were recorded: fall back to checking that
    # the pid still looks like Deskflow at all.
    if not _is_deskflow_process(_read_cmdline(pid)):
        decky.logger.warning("Recorded pid %s is not a Deskflow process, ignoring it", pid)
        return None
    return pid


class Plugin:
    def __init__(self):
        self._boot_task = None
        self._last_error = None
        # Held so the child can be waited on. Dropping the Popen leaves the
        # exited process unreaped, and an unreaped child answers signal 0, which
        # made every stop wait out the full grace period for a zombie.
        self._child = None

    # ------------------------------------------------------------------ settings
    def get_settings(self) -> dict:
        return _normalise(_read_json(_settings_path()))

    def set_settings(self, updates: dict) -> dict:
        settings = self.get_settings()
        for key, value in (updates or {}).items():
            if key in DEFAULT_SETTINGS:
                settings[key] = value
        settings = _normalise(settings)
        try:
            _write_json(_settings_path(), settings)
            self._last_error = None
        except OSError as err:
            decky.logger.error("Could not persist settings: %s", err)
            self._last_error = f"Could not save settings: {err}"
        return settings

    # ------------------------------------------------------------------- queries
    def get_configs(self) -> list:
        directory = _user_config_dir()
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            return []
        return [
            entry[: -len(".conf")]
            for entry in entries
            if entry.endswith(".conf") and os.path.isfile(os.path.join(directory, entry))
        ]

    def get_status(self) -> dict:
        settings = self.get_settings()
        processes = list(_iter_deskflow_processes())
        pids = [pid for pid, _ in processes]
        # Distinguishes "Deskflow is running because this plugin started it" from
        # "something else on the system is running Deskflow". Only the former can
        # be stopped from the panel, so the panel has to be able to tell them
        # apart rather than offering a Stop button that does nothing.
        recorded = _recorded_pid(_read_json(_state_path()))
        return {
            "running": bool(processes),
            "managed": recorded is not None and recorded in pids,
            "pids": pids,
            "processes": [cmdline for _, cmdline in processes],
            "role": settings["role"],
            "autostart": settings["autostart"],
            "launchers": {
                "client": _describe_launcher("client"),
                "server": _describe_launcher("server"),
            },
            "config_dir": _user_config_dir(),
            "last_error": self._last_error,
        }

    # -------------------------------------------------------------------- control
    async def start_deskflow(self) -> dict:
        return await asyncio.to_thread(self._start)

    async def stop_deskflow(self) -> dict:
        # Only Deskflow this plugin started is stopped. Sweeping every Deskflow
        # process on the machine would take down an instance the user launched
        # from Steam, which is exactly what this plugin promises not to do.
        return await asyncio.to_thread(self._stop)

    async def restart_deskflow(self) -> dict:
        return await asyncio.to_thread(self._restart)

    def _start(self) -> dict:
        settings = self.get_settings()
        try:
            argv, launcher = _build_argv(settings)
        except DeskflowNotFound as err:
            self._last_error = str(err)
            decky.logger.error("%s", err)
            return {"started": False, "error": str(err)}

        status = self.get_status()
        if status["running"]:
            if not status["managed"]:
                self._last_error = (
                    "Another Deskflow is already running, started outside this "
                    "plugin. Stop it first; this plugin will not terminate it."
                )
            else:
                self._last_error = "Deskflow is already running."
            return {"started": False, "error": self._last_error}

        log_path = _child_log_path()
        os.makedirs(_runtime_dir(), exist_ok=True)
        decky.logger.info("Starting %s: %s", launcher["label"], " ".join(argv))
        try:
            with open(log_path, "a", encoding="utf-8") as log_handle:
                log_handle.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start ===\n")
                log_handle.flush()
                process = subprocess.Popen(
                    argv,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=os.path.expanduser("~"),
                    start_new_session=True,
                )
        except (OSError, ValueError) as err:
            self._last_error = f"Could not start Deskflow: {err}"
            decky.logger.error("%s", err)
            return {"started": False, "error": self._last_error}

        self._child = process
        try:
            _write_json(
                _state_path(),
                {
                    "pid": process.pid,
                    "starttime": _proc_starttime(process.pid),
                    "argv": argv,
                    "label": launcher["label"],
                    "started_at": time.time(),
                },
            )
        except OSError as err:
            # Without a state file the plugin has no handle on this process, so it
            # could never stop it again. Take it back down rather than leak it.
            decky.logger.error("Could not record Deskflow state: %s", err)
            _terminate(process.pid, allow_group=True)
            self._child = None
            self._last_error = f"Could not start Deskflow: {err}"
            return {"started": False, "error": self._last_error}

        self._last_error = None
        decky.logger.info("Deskflow started with pid %s", process.pid)
        for _ in range(12):
            time.sleep(0.25)
            if process.poll() is not None:
                self._child = None
                self._last_error = _tail(log_path)
                decky.logger.error("Deskflow exited immediately with code %s", process.returncode)
                return {"started": False, "error": self._last_error or "Deskflow exited immediately."}
        return {"started": True, "pid": process.pid, "label": launcher["label"]}

    def _stop(self) -> dict:
        # Only ever the process this plugin started. A Deskflow the user launched
        # themselves is deliberately left alone, which is why there is no sweep
        # over other Deskflow processes here.
        state = _read_json(_state_path())
        stopped = []
        failed = []
        pid = _recorded_pid(state)

        if pid is not None:
            # Spawned by us, so its process group is ours to clean up.
            if _terminate(pid, allow_group=True):
                stopped.append(pid)
            else:
                failed.append(pid)

        if self._child is not None:
            try:
                self._child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            self._child = None

        if failed:
            # The state file is kept: it is still the only handle on that process,
            # so a later Stop can retry instead of the handle being lost.
            decky.logger.info("Stopped Deskflow processes: %s", stopped or "none")
            self._last_error = f"Could not stop Deskflow pid(s): {', '.join(map(str, failed))}"
            return {"stopped": False, "pids": stopped, "failed": failed, "error": self._last_error}

        try:
            os.remove(_state_path())
        except OSError:
            pass

        decky.logger.info("Stopped Deskflow processes: %s", stopped or "none")
        self._last_error = None
        return {"stopped": True, "pids": stopped}

    def _restart(self) -> dict:
        self._stop()
        return self._start()

    # ------------------------------------------------------------ plugin lifecycle
    async def _main(self):
        decky.logger.info("Deskflow Autostart loaded")
        settings = self.get_settings()
        if not settings["autostart"]:
            decky.logger.info("Autostart disabled, not launching Deskflow")
            return
        self._boot_task = asyncio.create_task(self._autostart())

    async def _autostart(self):
        try:
            await asyncio.sleep(AUTOSTART_DELAY)
            if self.get_settings()["autostart"]:
                result = await self.start_deskflow()
                if not result.get("started"):
                    decky.logger.error("Autostart did not launch Deskflow")
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - background task must never raise out
            decky.logger.error("Autostart failed: %s", err)

    async def _unload(self):
        if self._boot_task:
            self._boot_task.cancel()
            try:
                # Awaited so the cancellation is delivered before the loop closes;
                # a task abandoned mid-sleep is reported as destroyed-pending.
                await self._boot_task
            except asyncio.CancelledError:
                pass
            self._boot_task = None
        if _recorded_pid(_read_json(_state_path())) is not None:
            decky.logger.info("Unloading, stopping Deskflow started by this plugin")
            # Only our own process: a Deskflow the user started themselves is
            # left running.
            await asyncio.to_thread(self._stop)

    async def _uninstall(self):
        await asyncio.to_thread(self._stop)
        for path in (_state_path(), _child_log_path()):
            try:
                os.remove(path)
            except OSError:
                pass


def _send_signal(pid: int, sig: int, group: bool) -> bool:
    try:
        if group:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _terminate(pid: int, allow_group: bool = False) -> bool:
    """Signal one process, escalating from SIGTERM to SIGKILL.

    ``allow_group`` must only be True for a process this plugin spawned with
    ``start_new_session=True``. Any other process may share a process group with
    whatever launched it -- a Deskflow GUI started from Steam shares Steam's group
    -- so signalling that group would terminate Steam itself and drop the user
    back to the login screen. Everything else gets a single-pid signal.

    Returns True only when the process is gone by the end, so callers never
    report a process as stopped when the signal was refused or ignored.
    """
    if not isinstance(pid, int) or pid <= 1 or pid in _protected_pids():
        decky.logger.warning("Refusing to signal protected pid %s", pid)
        return False

    group = False
    if allow_group:
        try:
            # Only safe when the pid is genuinely its own group leader.
            group = os.getpgid(pid) == pid
        except (ProcessLookupError, OSError):
            group = False
        if not group:
            decky.logger.info("pid %s is not a group leader, signalling pid only", pid)

    for sig, wait in ((signal.SIGTERM, STOP_GRACE_PERIOD), (signal.SIGKILL, 1)):
        if not _send_signal(pid, sig, group):
            return not _alive(pid)
        deadline = time.time() + wait
        while time.time() < deadline:
            if not _alive(pid):
                return True
            time.sleep(0.2)
        _reap(pid)
        if not _alive(pid):
            return True
    decky.logger.warning("Deskflow pid %s did not exit", pid)
    return False


def _tail(path: str, lines: int = 4) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = [line.strip() for line in handle.readlines() if line.strip()]
    except OSError:
        return ""
    return " ".join(content[-lines:])[:300]
