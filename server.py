#!/usr/bin/env python3
"""Serve the on-printer filament manager.

Python 3.9's standard library is enough. The process listens on every
interface. Set FILAMENT_MANAGER_PORT before startup to change the port
(the default is 8091). There is no login, same as a stock QIDI Fluidd
and Moonraker on the local network.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

import catalog


INDEX = Path(__file__).resolve().parent / "index.html"
FAVICON = Path(__file__).resolve().parent / "favicon.png"
FENNEC = Path(__file__).resolve().parent / "fennec.svg"
MOONRAKER_URL = "http://127.0.0.1:7125/printer/objects/query?print_stats"
PRINTER_INFO_URL = "http://127.0.0.1:7125/printer/info"
FIRMWARE_RESTART_URL = "http://127.0.0.1:7125/printer/firmware_restart"
FLUIDD_THEME_URL = "http://127.0.0.1:7125/server/database/item?namespace=fluidd&key=uiSettings.theme"
DEFAULT_PRIMARY = "#2196F3"
CLIENT_UNIT = "qidi-client.service"
KLIPPER_UNIT = "klipper.service"
RESTART_COMMAND = ["/bin/systemctl", "restart", CLIENT_UNIT]
REBOOT_COMMAND = ["/bin/systemctl", "reboot"]
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
APPLIED_NAME = "klipper-applied.json"
IDLE_PRINT_STATES = ("standby", "complete", "cancelled", "error")
STOPPED_KLIPPY_STATES = ("shutdown", "error")
READY_TIMEOUT = 90.0
MAX_BODY = 1_000_000
NO_PRINT_STATE = "Moonraker on 127.0.0.1:7125 did not report print_stats.state."
NO_KLIPPY_STATE = "Moonraker on 127.0.0.1:7125 did not report Klipper's state."
PRINTER_BUSY = "The printer is busy. Make sure it is idle before applying changes."
STALE_RESTART = (
    "Klipper has not loaded the latest catalog. Use Apply Changes so Klipper and the touchscreen "
    "reload together. Selecting a new or renamed material after a touchscreen-only restart shuts Klipper down."
)


class ActionError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def listen_port(raw=None):
    if raw is None:
        raw = os.environ.get("FILAMENT_MANAGER_PORT", "8091")
    if not str(raw).isdigit():
        raise ValueError("FILAMENT_MANAGER_PORT must be a port number from 1 to 65535")
    port = int(raw)
    if port < 1 or port > 65535:
        raise ValueError("FILAMENT_MANAGER_PORT must be a port number from 1 to 65535")
    return port


def _systemctl_path():
    if Path("/bin/systemctl").exists():
        return "/bin/systemctl"
    return shutil.which("systemctl")


def client_state():
    systemctl = _systemctl_path()
    if not systemctl:
        return {"unit_installed": False, "active": None, "started": None}
    try:
        probe = subprocess.run(
            [systemctl, "cat", CLIENT_UNIT],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"unit_installed": False, "active": None, "started": None}
    if probe.returncode != 0:
        return {"unit_installed": False, "active": None, "started": None}
    active = None
    try:
        active_proc = subprocess.run(
            [systemctl, "is-active", CLIENT_UNIT],
            capture_output=True,
            text=True,
            timeout=5,
        )
        active = active_proc.stdout.strip() == "active"
    except (OSError, subprocess.TimeoutExpired):
        active = None
    return {
        "unit_installed": True,
        "active": active,
        "started": unit_started(systemctl, CLIENT_UNIT),
    }


def monotonic_to_wall(raw, now_wall=None, now_monotonic=None):
    """Convert a systemd monotonic stamp in microseconds to wall-clock seconds.

    The printer has no clock battery. Units that start before NTP sets the
    clock get wall-clock stamps that are minutes early, so systemd's own
    ActiveEnterTimestamp cannot be compared with file times.
    """
    try:
        micros = int(str(raw).strip())
    except ValueError:
        return None
    if micros <= 0:
        return None
    if now_wall is None:
        now_wall = time.time()
    if now_monotonic is None:
        now_monotonic = time.monotonic()
    return now_wall - (now_monotonic - micros / 1_000_000.0)


def unit_started(systemctl, unit):
    try:
        proc = subprocess.run(
            [systemctl, "show", unit, "-p", "ActiveEnterTimestampMonotonic"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    _key, _sep, raw = proc.stdout.strip().partition("=")
    return monotonic_to_wall(raw)


def boot_id(path=BOOT_ID_PATH):
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def read_applied(state_dir, current_boot):
    """Wall-clock time of the last Apply in this boot, or None."""
    if current_boot is None:
        return None
    try:
        data = json.loads((state_dir / APPLIED_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("boot_id") != current_boot:
        return None
    at = data.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return float(at)


def record_applied(state_dir, current_boot, at):
    if current_boot is None:
        return
    payload = json.dumps({"boot_id": current_boot, "at": at}) + "\n"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        temporary = state_dir / (APPLIED_NAME + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, state_dir / APPLIED_NAME)
    except OSError:
        return


def klipper_loaded_at(layout):
    """Latest time Klipper read the catalog: its service start, or the last Apply.

    A FIRMWARE_RESTART sent from the touchscreen or Fluidd also reloads it,
    but leaves nothing this app can see, so those are not counted.
    """
    systemctl = _systemctl_path()
    started = unit_started(systemctl, KLIPPER_UNIT) if systemctl else None
    applied = read_applied(layout.state_dir, boot_id())
    known = [value for value in (started, applied) if value is not None]
    return max(known) if known else None


def catalog_changed_since_start(catalog_path, started):
    if started is None:
        return None
    try:
        modified = catalog_path.stat().st_mtime
    except OSError:
        return None
    return modified > started + 1


def _hex_color(value):
    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) not in (4, 7, 9) or not text.startswith("#"):
        return False
    return all(char in "0123456789abcdefABCDEF" for char in text[1:])


def primary_from_theme(value):
    """Fluidd stores its primary color at uiSettings.theme.color."""
    if isinstance(value, dict):
        color = value.get("color")
        if isinstance(color, str):
            color = color.strip()
        if _hex_color(color):
            return color
    return DEFAULT_PRIMARY


def fluidd_primary(url=FLUIDD_THEME_URL):
    try:
        with urlopen(url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeError):
        return DEFAULT_PRIMARY
    result = payload.get("result")
    if not isinstance(result, dict):
        return DEFAULT_PRIMARY
    return primary_from_theme(result.get("value"))


def read_print_state(url=MOONRAKER_URL):
    try:
        with urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ActionError(NO_PRINT_STATE) from exc
    state = (
        payload.get("result", {})
        .get("status", {})
        .get("print_stats", {})
        .get("state")
    )
    if not isinstance(state, str) or state == "":
        raise ActionError(NO_PRINT_STATE)
    return state


def read_klippy_state(url=PRINTER_INFO_URL):
    """Return Klipper's state from printer.info: ready, startup, shutdown, or error."""
    try:
        with urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ActionError(NO_KLIPPY_STATE) from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    state = result.get("state") if isinstance(result, dict) else None
    if not isinstance(state, str) or state == "":
        raise ActionError(NO_KLIPPY_STATE)
    message = result.get("state_message")
    return state, message.strip() if isinstance(message, str) else ""


def idle_block(print_state, print_error):
    if print_state is None:
        return print_error or NO_PRINT_STATE
    if print_state not in IDLE_PRINT_STATES:
        return PRINTER_BUSY
    return None


def apply_block(print_state, print_error, klippy_state):
    """Apply is allowed when idle, and when Klipper has already shut down."""
    if klippy_state in STOPPED_KLIPPY_STATES:
        return None
    return idle_block(print_state, print_error)


def _sudo_permitted(command):
    try:
        probe = subprocess.run(
            ["sudo", "-n", "-l", *command],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActionError(f"could not check sudo permission: {exc}") from exc
    return probe.returncode == 0


def _klipper_stale(layout):
    if layout is None:
        return None
    return catalog_changed_since_start(layout.catalog, klipper_loaded_at(layout))


def restart_touchscreen(layout=None):
    state = client_state()
    if not state["unit_installed"]:
        raise ActionError("This printer has no qidi-client.service to restart.")
    if not Path("/bin/systemctl").exists():
        raise ActionError("This printer has no /bin/systemctl, so the touchscreen was not restarted.")
    block = idle_block(read_print_state(), None)
    if block:
        raise ActionError(block)
    if _klipper_stale(layout):
        raise ActionError(STALE_RESTART)
    _run_client_restart()
    return {
        "ok": True,
        "message": "Touchscreen client restarted. The screen stays blank for a few seconds. Klipper keeps running.",
    }


def _run_client_restart():
    try:
        proc = subprocess.run(
            ["sudo", "-n", *RESTART_COMMAND],
            capture_output=True,
            text=True,
            timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActionError(f"could not restart the touchscreen: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if "password is required" in detail.lower() or "not allowed" in detail.lower():
            raise ActionError(
                "The touchscreen restart was not permitted. "
                "install.sh adds a sudoers rule for /bin/systemctl restart qidi-client.service."
            )
        raise ActionError(detail or "could not restart the touchscreen")


def _post(url, timeout=10):
    try:
        with urlopen(Request(url, data=b"", method="POST"), timeout=timeout) as response:
            response.read()
    except (URLError, TimeoutError, OSError) as exc:
        raise ActionError(f"Moonraker did not accept {url}: {exc}") from exc


def wait_for_klipper(url=PRINTER_INFO_URL, timeout=READY_TIMEOUT, sleep=time.sleep, clock=time.monotonic):
    """Wait for Klipper to finish a FIRMWARE_RESTART and settle.

    Right after the request, printer.info can still report the state from
    before the restart, so a ready, shutdown, or error state only counts once
    Klipper was seen restarting, or after ten seconds.
    """
    began = clock()
    restarted = False
    while clock() - began < timeout:
        sleep(1.0)
        try:
            state, message = read_klippy_state(url)
        except ActionError:
            restarted = True
            continue
        if state not in ("ready",) + STOPPED_KLIPPY_STATES:
            restarted = True
            continue
        if restarted or clock() - began >= 10:
            return state, message
    raise ActionError(f"Klipper did not settle within {int(timeout)} seconds of FIRMWARE_RESTART.")


def apply_changes(
    layout,
    info_url=PRINTER_INFO_URL,
    restart_url=FIRMWARE_RESTART_URL,
    print_url=MOONRAKER_URL,
    wait=wait_for_klipper,
):
    """Restart Klipper so it rereads the catalog, then restart the touchscreen."""
    klippy_state, _message = read_klippy_state(info_url)
    print_state = None
    print_error = None
    if klippy_state not in STOPPED_KLIPPY_STATES:
        try:
            print_state = read_print_state(print_url)
        except ActionError as exc:
            print_error = str(exc)
    block = apply_block(print_state, print_error, klippy_state)
    if block:
        raise ActionError(block)
    client = client_state()
    restart_client = client["unit_installed"] and Path("/bin/systemctl").exists()
    if restart_client and not _sudo_permitted(RESTART_COMMAND):
        raise ActionError(
            "The touchscreen restart was not permitted, so Klipper was left alone. "
            "install.sh adds a sudoers rule for /bin/systemctl restart qidi-client.service."
        )
    _post(restart_url)
    state, message = wait(info_url)
    if state != "ready":
        detail = f" {message}" if message else ""
        raise ActionError(f"Klipper restarted but is {state}, so the touchscreen was left alone.{detail}")
    record_applied(layout.state_dir, boot_id(), time.time())
    if not restart_client:
        return {"ok": True, "message": "Klipper reloaded the catalog. This printer has no qidi-client.service to restart."}
    _run_client_restart()
    return {
        "ok": True,
        "message": "Klipper reloaded the catalog and the touchscreen restarted. New and renamed materials are safe to select.",
    }


def _run_reboot():
    try:
        subprocess.run(
            ["sudo", "-n", *REBOOT_COMMAND],
            capture_output=True,
            text=True,
            timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def prepare_reboot():
    if not Path("/bin/systemctl").exists():
        raise ActionError("This printer has no /bin/systemctl, so it was not rebooted.")
    block = idle_block(read_print_state(), None)
    if block:
        raise ActionError(block)
    if not _sudo_permitted(REBOOT_COMMAND):
        raise ActionError(
            "The printer reboot was not permitted. "
            "install.sh adds a sudoers rule for /bin/systemctl reboot. "
            "Run install.sh again if this printer was installed before that rule existed."
        )
    return {
        "ok": True,
        "message": "The printer is rebooting. This page disconnects until it is back. Klipper restarts with it.",
    }


def arm_reboot():
    timer = threading.Timer(2.0, _run_reboot)
    timer.daemon = True
    timer.start()


def build_status(layout, homes):
    primary = fluidd_primary()
    if layout is None:
        return {
            "found": False,
            "message": catalog.missing_catalog_message(homes),
            "primary": primary,
        }
    client = client_state()
    stale = catalog_changed_since_start(layout.catalog, client["started"]) if client["unit_installed"] else None
    klipper_stale = _klipper_stale(layout)
    systemctl = Path("/bin/systemctl").exists()
    print_state = None
    print_error = None
    try:
        print_state = read_print_state()
    except ActionError as exc:
        print_error = str(exc)
    try:
        klippy_state, _message = read_klippy_state()
    except ActionError:
        klippy_state = None
    if not client["unit_installed"]:
        restart_block = "This printer has no qidi-client.service to restart."
    else:
        restart_block = idle_block(print_state, print_error)
        if restart_block is None and klipper_stale:
            restart_block = STALE_RESTART
    if not systemctl:
        reboot_block = "This printer has no /bin/systemctl, so it cannot be rebooted from this page."
    else:
        reboot_block = idle_block(print_state, print_error)
    if klippy_state is None:
        apply_reason = NO_KLIPPY_STATE
    else:
        apply_reason = apply_block(print_state, print_error, klippy_state)
    try:
        count = len(json.loads(layout.catalog.read_bytes().decode("utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        count = None
    return {
        "found": True,
        "catalog": str(layout.catalog),
        "config_dir": str(layout.config_dir),
        "home": str(layout.home),
        "owner": layout.owner,
        "count": count,
        "stale": stale,
        "klipper_stale": klipper_stale,
        "klippy_state": klippy_state,
        "client_unit": client["unit_installed"],
        "client_active": client["active"],
        "print_state": print_state,
        "apply_allowed": apply_reason is None,
        "apply_block": apply_reason,
        "restart_allowed": restart_block is None,
        "restart_block": restart_block,
        "reboot_allowed": reboot_block is None,
        "reboot_block": reboot_block,
        "primary": primary,
    }


def _token(data, key, label):
    value = data.get(key)
    if isinstance(value, str):
        value = value.strip()
    return catalog.safe_token(value, label)


def _optional_chamber(data):
    if "chamber" not in data or data["chamber"] is None or data["chamber"] == "":
        return None
    value = data["chamber"]
    if isinstance(value, bool):
        raise catalog.CatalogError("chamber must be a string")
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str):
        value = value.strip()
    return catalog.safe_token(value, "chamber")


def _optional_nozzle(data):
    low = data.get("nozzle_min", None)
    high = data.get("nozzle_max", None)
    if low in (None, "") and high in (None, ""):
        return None
    if low in (None, "") or high in (None, ""):
        raise catalog.CatalogError("nozzle minimum and maximum are both required")
    return (low, high)


def _identity(data, include_original):
    fields = {
        "name": _token(data, "name", "name"),
        "material_type": _token(data, "type", "type"),
        "filament_id": _token(data, "id", "id"),
        "vendor": _token(data, "vendor", "vendor"),
        "nozzle": _optional_nozzle(data),
        "chamber": _optional_chamber(data),
    }
    if include_original:
        fields["original_name"] = _token(data, "original", "original")
    else:
        fields["template"] = _token(data, "template", "template")
    return fields


class Handler(BaseHTTPRequestHandler):
    server_version = "filament-manager"

    def log_message(self, fmt, *args):
        sys.stdout.write("%s\n" % (fmt % args))
        sys.stdout.flush()

    def do_GET(self):
        self._dispatch(self.route_get)

    def do_POST(self):
        self._dispatch(self.route_post)

    def _dispatch(self, route):
        try:
            route()
        except catalog.CatalogError as exc:
            self.send_json(400, {"error": str(exc)})
        except ActionError as exc:
            self.send_json(exc.status, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "Something went wrong while handling that request."})

    def route_get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/favicon.ico", "/favicon.png"):
            self.serve_favicon()
            return
        if path == "/fennec.svg":
            self.serve_fennec()
            return
        if path == "/":
            self.serve_index()
            return
        if path == "/api/status":
            self.send_json(200, build_status(self.layout(), self.server.homes))
            return
        if path == "/api/filaments":
            self.serve_filaments()
            return
        if path == "/api/backups":
            self.serve_backups()
            return
        prefix = "/api/backups/"
        if path.startswith(prefix):
            name = unquote(path[len(prefix):])
            where = parse_qs(parsed.query).get("where", ["config"])[0]
            self.serve_backup_download(name, where)
            return
        self.send_json(404, {"error": "not found"})

    def route_post(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/filaments":
            body = self.read_json()
            fields = _identity(body, include_original=False)
            result = self._mutate(
                lambda layout: catalog.add_filament(
                    layout,
                    fields["template"],
                    fields["name"],
                    fields["material_type"],
                    fields["filament_id"],
                    fields["vendor"],
                    nozzle=fields["nozzle"],
                    chamber=fields["chamber"],
                )
            )
            self.send_json(200, result)
            return
        if path == "/api/filaments/update":
            body = self.read_json()
            fields = _identity(body, include_original=True)
            result = self._mutate(
                lambda layout: catalog.update_filament(
                    layout,
                    fields["original_name"],
                    fields["name"],
                    fields["material_type"],
                    fields["filament_id"],
                    fields["vendor"],
                    nozzle=fields["nozzle"],
                    chamber=fields["chamber"],
                )
            )
            self.send_json(200, result)
            return
        if path == "/api/filaments/delete":
            body = self.read_json()
            name = _token(body, "name", "name")
            result = self._mutate(lambda layout: catalog.delete_filament(layout, name))
            self.send_json(200, result)
            return
        if path == "/api/backup":
            result = self._mutate(catalog.create_backup)
            self.send_json(200, result)
            return
        if path == "/api/backups/delete":
            body = self.read_json()
            name = body.get("name")
            result = self._mutate(lambda layout: catalog.delete_backup(layout, name))
            self.send_json(200, result)
            return
        if path == "/api/restore":
            body = self.read_json()
            name = body.get("name")
            where = body.get("where")
            result = self._mutate(lambda layout: catalog.restore_backup(layout, name, where))
            self.send_json(200, result)
            return
        if path == "/api/restart":
            self.send_json(200, restart_touchscreen(self.layout()))
            return
        if path == "/api/apply":
            layout = self.require_layout()
            with self.server.apply_lock:
                self.send_json(200, apply_changes(layout))
            return
        if path == "/api/reboot":
            payload = prepare_reboot()
            self.send_json(200, payload)
            arm_reboot()
            return
        self.send_json(404, {"error": "not found"})

    def layout(self):
        return catalog.discover(self.server.homes)

    def require_layout(self):
        layout = self.layout()
        if layout is None:
            raise catalog.CatalogError(catalog.missing_catalog_message(self.server.homes))
        return layout

    def _mutate(self, operation):
        layout = self.require_layout()
        with self.server.catalog_lock:
            result = operation(layout)
        return result.as_dict()

    def serve_filaments(self):
        layout = self.layout()
        if layout is None:
            self.send_json(200, {
                "found": False,
                "count": 0,
                "filaments": [],
                "message": catalog.missing_catalog_message(self.server.homes),
            })
            return
        with self.server.catalog_lock:
            payload = catalog.filament_rows(layout)
        payload["catalog"] = str(layout.catalog)
        self.send_json(200, payload)

    def serve_backups(self):
        layout = self.require_layout()
        with self.server.catalog_lock:
            rows = catalog.list_backups(layout)
        self.send_json(200, {"backups": rows})

    def serve_backup_download(self, name, where):
        layout = self.require_layout()
        with self.server.catalog_lock:
            _path, payload = catalog.read_backup(layout, name, where)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def serve_index(self):
        try:
            payload = INDEX.read_bytes()
        except OSError:
            self.send_json(500, {"error": "index.html is missing"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def serve_favicon(self):
        try:
            payload = FAVICON.read_bytes()
        except OSError:
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def serve_fennec(self):
        try:
            payload = FENNEC.read_bytes()
        except OSError:
            self.send_json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self):
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise catalog.CatalogError("expected a JSON body")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise catalog.CatalogError("expected a JSON body") from exc
        if length < 0 or length > MAX_BODY:
            raise catalog.CatalogError("request is too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            raise catalog.CatalogError("expected a JSON body")
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise catalog.CatalogError("Content-Type must be application/json")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise catalog.CatalogError("body is not JSON") from exc
        if not isinstance(data, dict):
            raise catalog.CatalogError("JSON body must be an object")
        return data

    def send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class ManagerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, homes=None, ipv6=False):
        self.homes = homes
        self.ipv6 = ipv6
        self.catalog_lock = threading.Lock()
        self.apply_lock = threading.Lock()
        if ipv6:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, Handler)

    def server_bind(self):
        if self.ipv6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def create_server(host, port, homes=None, ipv6=False):
    return ManagerServer((host, port), homes=homes, ipv6=ipv6)


def main():
    try:
        port = listen_port()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        httpd = create_server("::", port, ipv6=True)
    except OSError:
        try:
            httpd = create_server("0.0.0.0", port, ipv6=False)
        except OSError as exc:
            print(f"could not listen on port {port}: {exc}", file=sys.stderr)
            return 1
    print(f"Filament Manager listening on port {port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
