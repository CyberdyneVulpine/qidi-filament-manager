#!/usr/bin/env python3
"""Read and edit the QIDI touchscreen filament catalog.

The load menu reads filament-0.4.json from the touchscreen client. Current
images keep that file under /home/qidi. Older images use /home/mks. This
module finds whichever of those homes has the file.

Adds, edits, and deletes splice the file as text so the rest of the JSON
keeps its formatting. Hiding parks that same text in the state directory and
splices it back on unhide. Each write is parsed again and discarded if any
entry other than the one being changed would not match the file that was read.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import shutil
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows has no fcntl; the printer does.
    fcntl = None


CATALOG_RELATIVE = Path("QIDI_Client/resource/filament_config/filament-0.4.json")
CONFIG_RELATIVE = Path("printer_data/config")
STATE_RELATIVE = Path("filament-manager/state")
BASELINE_NAME = "original-ids.json"
HIDDEN_NAME = "hidden.json"
INSTALL_BACKUP_NAME = "install-backup.json"
BACKUP_NAME = re.compile(r"^filament-0\.4\.json\.bak-[0-9]{8}_[0-9]{6}(?:-[0-9]+)?$")
NOZZLE_PATTERN = re.compile(
    r'("nozzle_temperature": \[\s*)(-?\d+(?:\.\d+)?)(\s*,\s*)(-?\d+(?:\.\d+)?)(\s*\])'
)
SAVED_VARIABLES_NAME = "saved_variables.cfg"
SLOT_PATTERN = re.compile(r"^filament_slot(\d+)\s*=\s*(.+?)\s*$")
EXTERNAL_SLOT = 16


class CatalogError(Exception):
    """The catalog was not changed."""


class Layout:
    def __init__(self, home, catalog, config_dir, state_dir):
        self.home = home
        self.catalog = catalog
        self.config_dir = config_dir
        self.state_dir = state_dir

    @property
    def owner(self):
        return self.home.name


class ChangeResult:
    def __init__(self, changed, message, beside=None, config_copy=None, before_count=0, after_count=0):
        self.changed = changed
        self.message = message
        self.beside = beside
        self.config_copy = config_copy
        self.before_count = before_count
        self.after_count = after_count

    def as_dict(self):
        return {
            "ok": True,
            "changed": self.changed,
            "message": self.message,
            "before": self.before_count,
            "after": self.after_count,
            "backup_beside": None if self.beside is None else str(self.beside),
            "backup_config": None if self.config_copy is None else str(self.config_copy),
        }


def default_homes():
    return [Path("/home/qidi"), Path("/home/mks")]


def homes_to_search(homes=None):
    if homes is None:
        return default_homes()
    return list(homes)


def missing_catalog_message(homes=None):
    paths = [(home / CATALOG_RELATIVE).as_posix() for home in homes_to_search(homes)]
    if len(paths) == 1:
        joined = paths[0]
    elif len(paths) == 2:
        joined = f"{paths[0]} and {paths[1]}"
    else:
        joined = ", ".join(paths[:-1]) + ", and " + paths[-1]
    return (
        "This printer's touchscreen does not use that catalog. "
        f"Looked for {joined}."
    )


def discover(homes=None):
    for home in homes_to_search(homes):
        catalog = home / CATALOG_RELATIVE
        if catalog.is_file():
            return Layout(
                home=home,
                catalog=catalog,
                config_dir=home / CONFIG_RELATIVE,
                state_dir=home / STATE_RELATIVE,
            )
    return None


def require_layout(homes=None):
    layout = discover(homes)
    if layout is None:
        raise CatalogError(missing_catalog_message(homes))
    return layout


def safe_token(value, label):
    if (
        not isinstance(value, str)
        or value == ""
        or any(ord(char) < 32 or char in '"\\' for char in value)
    ):
        raise CatalogError(
            f"{label} must be a non-empty string without quotes, backslashes, or control characters"
        )
    return value


def require_composed_name(name, vendor):
    """The load list uses the first word of the catalog name as the vendor.

    After a row is selected, the screen shows filament_vendor and the full
    name. Those agree when the name is the vendor, one space, and the material.
    """
    if any(char.isspace() for char in vendor):
        raise CatalogError(
            "vendor must be a single word. The load list uses the first word of the name as the vendor."
        )
    prefix = vendor + " "
    material = name[len(prefix):] if name.startswith(prefix) else ""
    if material == "" or material != material.strip():
        raise CatalogError(
            f'name must be "{vendor}" followed by one space and the material, '
            "so the load list and the saved vendor match."
        )
    return name


def _read_text(path):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CatalogError(f"could not read {path}: {exc}") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogError(f"{path.name} is not UTF-8") from exc


def read_catalog(layout):
    text = _read_text(layout.catalog)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"catalog is not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise CatalogError("catalog is not a JSON object")
    return text, data


def _write_bytes(path, payload):
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(payload)
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise CatalogError(f"could not write {path.name}: {exc}") from exc


@contextmanager
def _file_lock(layout):
    """Serialize catalog edits on the printer. No-op where fcntl is missing."""
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    handle = (layout.state_dir / "write.lock").open("a+")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _baseline_path(layout):
    return layout.state_dir / BASELINE_NAME


def _load_baseline(path):
    try:
        data = json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"could not read recorded filament ids in {path}") from exc
    ids = data.get("ids") if isinstance(data, dict) else None
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise CatalogError(f"could not read recorded filament ids in {path}")
    return set(ids)


def _ids_in(data):
    found = set()
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        filament_id = entry.get("filament_id")
        if filament_id in (None, ""):
            continue
        found.add(str(filament_id))
    return found


def slot_label(number):
    if number == EXTERNAL_SLOT:
        return "the external spool"
    return f"box {number // 4 + 1} slot {number % 4 + 1}"


def slots_in_use(layout):
    """Map each catalog id that Klipper has saved on a slot to those slot numbers.

    Box slots that were never set hold the integer 1, not a catalog id.
    """
    path = layout.config_dir / SAVED_VARIABLES_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    found = {}
    for line in text.splitlines():
        match = SLOT_PATTERN.match(line.strip())
        if match is None:
            continue
        try:
            value = ast.literal_eval(match.group(2))
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str) and value != "":
            found.setdefault(value, []).append(int(match.group(1)))
    for numbers in found.values():
        numbers.sort()
    return found


def _require_not_in_use(slots, filament_id, name, action):
    numbers = slots.get(filament_id)
    if not numbers:
        return
    where = ", ".join(slot_label(number) for number in numbers)
    raise CatalogError(
        f"{name} ({filament_id}) is selected on {where}. Klipper shuts down when it loads a slot "
        f"whose id is not in the catalog. Choose another material for that slot on the touchscreen, "
        f"then {action}."
    )


def ensure_baseline(layout, data):
    """Record ids from the first run. Later ids are the ones added here."""
    path = _baseline_path(layout)
    if path.is_file():
        return _load_baseline(path)
    payload = (json.dumps({"ids": sorted(_ids_in(data))}, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return _load_baseline(path)
    except OSError as exc:
        raise CatalogError(f"could not record filament ids: {exc}") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    return _load_baseline(path)


def _text_field(entry, key):
    if not isinstance(entry, dict):
        return ""
    value = entry.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _nozzle_bounds(entry):
    if not isinstance(entry, dict):
        return None, None
    nozzle = entry.get("nozzle_temperature")
    if not isinstance(nozzle, list) or len(nozzle) < 2:
        return None, None
    low, high = nozzle[0], nozzle[1]
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in (low, high)):
        return None, None
    return low, high


def _chamber_text(entry):
    return _text_field(entry, "chamber_temperatures")


def _added_here(entry, baseline):
    filament_id = _text_field(entry, "filament_id")
    if filament_id == "":
        return False
    return filament_id not in baseline


def public_row(name, entry, baseline, slots=None, hidden=False):
    low, high = _nozzle_bounds(entry)
    filament_id = _text_field(entry, "filament_id")
    numbers = (slots or {}).get(filament_id, []) if filament_id else []
    return {
        "name": name,
        "type": _text_field(entry, "type"),
        "id": filament_id,
        "vendor": _text_field(entry, "filament_vendor"),
        "nozzle_min": low,
        "nozzle_max": high,
        "chamber": _chamber_text(entry),
        "added_here": _added_here(entry, baseline),
        "in_use": [slot_label(number) for number in numbers],
        "hidden": hidden,
    }


def filament_rows(layout):
    with _file_lock(layout):
        _text, data = read_catalog(layout)
        baseline = ensure_baseline(layout, data)
        slots = slots_in_use(layout)
        blocks = _parked_blocks(layout, data)
        rows = [public_row(name, entry, baseline, slots) for name, entry in data.items()]
        for name, block in blocks.items():
            rows.append(public_row(name, _entry_from_block(name, block), baseline, slots, hidden=True))
    return {"found": True, "count": len(data), "filaments": rows}


def object_span(text, key):
    """Return the source slice of one top-level entry, including a trailing comma."""
    marker = f'  "{key}": {{'
    start = text.find(marker)
    if start < 0:
        return None
    depth = 0
    opened = False
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
            opened = True
        elif char == "}":
            depth -= 1
            if opened and depth == 0:
                end = index + 1
                if text[end:end + 1] == ",":
                    end += 1
                return start, end
    return None


def _replace_once(block, old, new, label):
    if old == new:
        return block
    if block.count(old) != 1:
        raise CatalogError(f"template block does not contain exactly one {label}")
    return block.replace(old, new, 1)


def _apply_nozzle(block, nozzle):
    match = NOZZLE_PATTERN.search(block)
    if match is None:
        raise CatalogError("could not find the nozzle_temperature array in the template")
    low, high = nozzle
    return (
        block[:match.start()]
        + f"{match.group(1)}{low}{match.group(3)}{high}{match.group(5)}"
        + block[match.end():]
    )


def _rewrite_block(block, source_key, source, new_key, new_type, new_id, new_vendor, nozzle, chamber):
    rewritten = block
    rewritten = _replace_once(rewritten, f'"{source_key}"', f'"{new_key}"', "entry name")
    rewritten = _replace_once(
        rewritten,
        f'"type": "{source.get("type", "")}"',
        f'"type": "{new_type}"',
        "type",
    )
    rewritten = _replace_once(
        rewritten,
        f'"filament_id": "{source.get("filament_id", "")}"',
        f'"filament_id": "{new_id}"',
        "filament_id",
    )
    rewritten = _replace_once(
        rewritten,
        f'"filament_vendor": "{source.get("filament_vendor", "")}"',
        f'"filament_vendor": "{new_vendor}"',
        "filament_vendor",
    )
    if chamber is not None:
        rewritten = _replace_once(
            rewritten,
            f'"chamber_temperatures": "{source.get("chamber_temperatures", "")}"',
            f'"chamber_temperatures": "{chamber}"',
            "chamber_temperatures",
        )
    if nozzle is not None:
        rewritten = _apply_nozzle(rewritten, nozzle)
    return rewritten


def _expected_entry(source, new_type, new_id, new_vendor, nozzle, chamber):
    entry = copy.deepcopy(source)
    entry["type"] = new_type
    entry["filament_id"] = new_id
    entry["filament_vendor"] = new_vendor
    if nozzle is not None:
        entry["nozzle_temperature"] = [nozzle[0], nozzle[1]]
    if chamber is not None:
        entry["chamber_temperatures"] = str(chamber)
    return entry


def _whole_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CatalogError(f"{label} must be a whole number")
    if isinstance(value, float) and not value.is_integer():
        raise CatalogError(f"{label} must be a whole number")
    return int(value)


def normalize_nozzle(nozzle):
    if nozzle is None:
        return None
    if not isinstance(nozzle, (tuple, list)) or len(nozzle) != 2:
        raise CatalogError("nozzle minimum and maximum are both required")
    low = _whole_number(nozzle[0], "nozzle minimum")
    high = _whole_number(nozzle[1], "nozzle maximum")
    if low > high:
        raise CatalogError("nozzle minimum is higher than nozzle maximum")
    return (low, high)


def _require_object(data, key, label):
    if key not in data:
        raise CatalogError(f"{label} not found: {key}")
    if not isinstance(data[key], dict):
        raise CatalogError(f"{label} is not an object: {key}")
    return data[key]


def _insert_after(text, end, block, cloned):
    if block.endswith(","):
        return text[:end] + "\n" + cloned + text[end:]
    return text[:end] + ",\n" + cloned + text[end:]


def _removal_bounds(text, key):
    span = object_span(text, key)
    if span is None:
        return None
    start, end = span
    had_comma = text[end - 1] == ","
    if start > 0 and text[start - 1] == "\n":
        start -= 1
        if start > 0 and text[start - 1] == "\r":
            start -= 1
    # The last entry has no comma of its own. Drop the previous entry's comma
    # so the file does not end with a trailing comma.
    if not had_comma and start > 0 and text[start - 1] == ",":
        start -= 1
    return start, end


def ensure_untouched(original, updated, touched):
    missing = []
    changed = []
    for name, entry in original.items():
        if name in touched:
            continue
        if name not in updated:
            missing.append(name)
        elif updated[name] != entry:
            changed.append(name)
    extra = [name for name in updated if name not in original and name not in touched]
    names = changed + missing + extra
    if names:
        preview = ", ".join(names[:5])
        raise CatalogError(f"refusing to save because an untouched entry would change: {preview}")


def _parse_result(updated_text):
    try:
        updated = json.loads(updated_text)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"refusing to save because the result is not valid JSON ({exc})") from exc
    if not isinstance(updated, dict):
        raise CatalogError("refusing to save because the result is not a JSON object")
    return updated


def _allocate_backup_name(layout):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{layout.catalog.name}.bak-{stamp}"
    candidate = base
    number = 2
    while True:
        beside = layout.catalog.parent / candidate
        config_copy = layout.config_dir / candidate
        if not beside.exists() and not config_copy.exists():
            return candidate
        candidate = f"{base}-{number}"
        number += 1
        if number > 1000:
            raise CatalogError("could not choose a backup name")


def create_backup(layout):
    with _file_lock(layout):
        if not layout.catalog.is_file():
            raise CatalogError(missing_catalog_message([layout.home]))
        name = _allocate_backup_name(layout)
        beside = layout.catalog.parent / name
        config_copy = layout.config_dir / name
        try:
            layout.config_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(layout.catalog, beside)
            try:
                shutil.copy2(layout.catalog, config_copy)
            except OSError:
                beside.unlink()
                raise
        except OSError as exc:
            raise CatalogError(f"could not back up the catalog: {exc}") from exc
        try:
            count = len(json.loads(_read_text(layout.catalog)))
        except (CatalogError, json.JSONDecodeError, TypeError):
            count = 0
        return ChangeResult(
            changed=True,
            message="Saved a copy beside the catalog and in printer_data/config.",
            beside=beside,
            config_copy=config_copy,
            before_count=count,
            after_count=count,
        )


def _commit(layout, updated_text, updated):
    name = _allocate_backup_name(layout)
    beside = layout.catalog.parent / name
    config_copy = layout.config_dir / name
    try:
        layout.config_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(layout.catalog, beside)
        try:
            shutil.copy2(layout.catalog, config_copy)
        except OSError:
            beside.unlink()
            raise
        _write_bytes(layout.catalog, updated_text.encode("utf-8"))
    except OSError as exc:
        raise CatalogError(f"could not write the catalog: {exc}") from exc
    try:
        confirmed = json.loads(_read_text(layout.catalog))
    except (CatalogError, json.JSONDecodeError) as exc:
        shutil.copy2(beside, layout.catalog)
        raise CatalogError("catalog read-back was not valid JSON; restored the backup") from exc
    if confirmed != updated:
        shutil.copy2(beside, layout.catalog)
        raise CatalogError("catalog read-back did not match; restored the backup")
    return beside, config_copy


def _hidden_path(layout):
    return layout.state_dir / HIDDEN_NAME


def _load_hidden_blocks(layout):
    path = _hidden_path(layout)
    if not path.is_file():
        return {}
    try:
        data = json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"could not read hidden materials in {path.name}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"could not read hidden materials in {path.name}")
    for name, block in data.items():
        if not isinstance(name, str) or not isinstance(block, str):
            raise CatalogError(f"could not read hidden materials in {path.name}")
    return data


def _write_hidden_blocks(layout, blocks):
    payload = (json.dumps(blocks, indent=2) + "\n").encode("utf-8")
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    _write_bytes(_hidden_path(layout), payload)


def _hidden_snapshot(layout):
    path = _hidden_path(layout)
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CatalogError(f"could not read hidden materials in {path.name}") from exc


def _restore_hidden(layout, snapshot):
    path = _hidden_path(layout)
    if snapshot is None:
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            raise CatalogError(f"could not restore hidden materials: {exc}") from exc
        return
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    _write_bytes(path, snapshot)


def _reconcile_hidden(layout, catalog_data, blocks):
    """Drop parked copies whose names are back in the catalog. The catalog wins."""
    if not any(name in catalog_data for name in blocks):
        return blocks
    kept = {name: block for name, block in blocks.items() if name not in catalog_data}
    _write_hidden_blocks(layout, kept)
    return kept


def _parked_blocks(layout, catalog_data):
    return _reconcile_hidden(layout, catalog_data, _load_hidden_blocks(layout))


def _entry_from_block(name, block):
    try:
        parsed = json.loads("{" + block + "}")
    except json.JSONDecodeError as exc:
        raise CatalogError(f"hidden material {name} is not a catalog entry") from exc
    entry = parsed.get(name) if isinstance(parsed, dict) else None
    if not isinstance(parsed, dict) or list(parsed) != [name] or not isinstance(entry, dict):
        raise CatalogError(f"hidden material {name} is not a catalog entry")
    return entry


def _hidden_entries(blocks):
    return {name: _entry_from_block(name, block) for name, block in blocks.items()}


def _stored_block(text, key):
    span = object_span(text, key)
    if span is None:
        return None
    start, end = span
    block = text[start:end]
    if block.endswith(","):
        block = block[:-1]
    return block


def _insert_at_end(text, original, block):
    if not original:
        raise CatalogError("the catalog is empty")
    last_key = next(reversed(original))
    span = object_span(text, last_key)
    if span is None:
        raise CatalogError(f"could not locate the {last_key} block in the file text")
    start, end = span
    return _insert_after(text, end, text[start:end], block)


def _ids_owned_by_others(data, filament_id, allowed_name, hidden=None):
    owners = [
        name
        for name, entry in data.items()
        if isinstance(entry, dict) and entry.get("filament_id") == filament_id and name != allowed_name
    ]
    for name, entry in (hidden or {}).items():
        if name == allowed_name or not isinstance(entry, dict):
            continue
        if entry.get("filament_id") == filament_id:
            owners.append(f"{name} (hidden)")
    if owners:
        raise CatalogError(f"{filament_id} is already used by {', '.join(owners)}")


def add_filament(layout, template, name, material_type, filament_id, vendor, nozzle=None, chamber=None):
    template = safe_token(template, "template")
    new_key = safe_token(name, "name")
    new_type = safe_token(material_type, "type")
    new_id = safe_token(filament_id, "id")
    new_vendor = safe_token(vendor, "vendor")
    new_key = require_composed_name(new_key, new_vendor)
    if chamber is not None:
        chamber = safe_token(str(chamber), "chamber")
    nozzle = normalize_nozzle(nozzle)

    with _file_lock(layout):
        text, original = read_catalog(layout)
        ensure_baseline(layout, original)
        blocks = _parked_blocks(layout, original)
        if new_key in blocks:
            raise CatalogError(f"{new_key} is hidden. Unhide it before adding another with that name.")
        _ids_owned_by_others(original, new_id, new_key, _hidden_entries(blocks))
        if template in original:
            source = _require_object(original, template, "template entry")
            template_block = None
        elif template in blocks:
            source = _entry_from_block(template, blocks[template])
            template_block = blocks[template]
        else:
            raise CatalogError(f"template entry not found: {template}")
        wanted = _expected_entry(source, new_type, new_id, new_vendor, nozzle, chamber)
        if new_key in original:
            if original[new_key] == wanted:
                return ChangeResult(
                    changed=False,
                    message=f"{new_key} ({new_id}) is already present",
                    before_count=len(original),
                    after_count=len(original),
                )
            raise CatalogError(f"{new_key} exists but does not match this definition")

        if template_block is None:
            span = object_span(text, template)
            if span is None:
                raise CatalogError(f"could not locate the {template} block in the file text")
            start, end = span
            block = text[start:end]
            cloned = _rewrite_block(block, template, source, new_key, new_type, new_id, new_vendor, nozzle, chamber)
            updated_text = _insert_after(text, end, block, cloned)
        else:
            cloned = _rewrite_block(
                template_block, template, source, new_key, new_type, new_id, new_vendor, nozzle, chamber
            )
            if cloned.endswith(","):
                cloned = cloned[:-1]
            updated_text = _insert_at_end(text, original, cloned)
        updated = _parse_result(updated_text)
        if updated.get(new_key) != wanted:
            raise CatalogError("new entry does not match the requested definition")
        ensure_untouched(original, updated, {new_key})
        if len(updated) != len(original) + 1:
            raise CatalogError("entry count did not increase by one")
        beside, config_copy = _commit(layout, updated_text, updated)
        return ChangeResult(
            changed=True,
            message=f"Added {new_key} ({new_id}).",
            beside=beside,
            config_copy=config_copy,
            before_count=len(original),
            after_count=len(updated),
        )


def update_filament(layout, original_name, name, material_type, filament_id, vendor, nozzle=None, chamber=None):
    original_name = safe_token(original_name, "original")
    new_key = safe_token(name, "name")
    new_type = safe_token(material_type, "type")
    new_id = safe_token(filament_id, "id")
    new_vendor = safe_token(vendor, "vendor")
    new_key = require_composed_name(new_key, new_vendor)
    if chamber is not None:
        chamber = safe_token(str(chamber), "chamber")
    nozzle = normalize_nozzle(nozzle)

    with _file_lock(layout):
        text, original = read_catalog(layout)
        ensure_baseline(layout, original)
        blocks = _parked_blocks(layout, original)
        source = _require_object(original, original_name, "entry")
        if new_key != original_name and new_key in original:
            raise CatalogError(f"{new_key} already exists")
        if new_key != original_name and new_key in blocks:
            raise CatalogError(f"{new_key} is hidden. Unhide it before using that name.")
        _ids_owned_by_others(original, new_id, original_name, _hidden_entries(blocks))
        old_id = _text_field(source, "filament_id")
        if old_id and old_id != new_id:
            _require_not_in_use(slots_in_use(layout), old_id, original_name, "change its id")
        wanted = _expected_entry(source, new_type, new_id, new_vendor, nozzle, chamber)
        if new_key == original_name and original[original_name] == wanted:
            return ChangeResult(
                changed=False,
                message=f"{new_key} is unchanged.",
                before_count=len(original),
                after_count=len(original),
            )
        span = object_span(text, original_name)
        if span is None:
            raise CatalogError(f"could not locate the {original_name} block in the file text")
        start, end = span
        rewritten = _rewrite_block(
            text[start:end],
            original_name,
            source,
            new_key,
            new_type,
            new_id,
            new_vendor,
            nozzle,
            chamber,
        )
        updated_text = text[:start] + rewritten + text[end:]
        updated = _parse_result(updated_text)
        if original_name != new_key and original_name in updated:
            raise CatalogError("old name is still present")
        if updated.get(new_key) != wanted:
            raise CatalogError("updated entry does not match the requested definition")
        ensure_untouched(original, updated, {original_name, new_key})
        if len(updated) != len(original):
            raise CatalogError("entry count changed unexpectedly")
        beside, config_copy = _commit(layout, updated_text, updated)
        if original_name == new_key:
            message = f"Updated {new_key}."
        else:
            message = f"Renamed {original_name} to {new_key}."
        return ChangeResult(
            changed=True,
            message=message,
            beside=beside,
            config_copy=config_copy,
            before_count=len(original),
            after_count=len(updated),
        )


def delete_filament(layout, name):
    key = safe_token(name, "name")
    with _file_lock(layout):
        text, original = read_catalog(layout)
        ensure_baseline(layout, original)
        _parked_blocks(layout, original)
        entry = _require_object(original, key, "entry")
        if len(original) <= 1:
            raise CatalogError("the catalog cannot be emptied")
        filament_id = _text_field(entry, "filament_id")
        if filament_id:
            _require_not_in_use(slots_in_use(layout), filament_id, key, "remove it")
        bounds = _removal_bounds(text, key)
        if bounds is None:
            raise CatalogError(f"could not locate the {key} block in the file text")
        start, end = bounds
        updated_text = text[:start] + text[end:]
        updated = _parse_result(updated_text)
        if key in updated:
            raise CatalogError("entry is still present")
        ensure_untouched(original, updated, {key})
        if len(updated) != len(original) - 1:
            raise CatalogError("entry count did not decrease by one")
        beside, config_copy = _commit(layout, updated_text, updated)
        return ChangeResult(
            changed=True,
            message=f"Removed {key}.",
            beside=beside,
            config_copy=config_copy,
            before_count=len(original),
            after_count=len(updated),
        )


def hide_filament(layout, name):
    """Move one catalog entry into the state directory so the load menu skips it."""
    key = safe_token(name, "name")
    with _file_lock(layout):
        text, original = read_catalog(layout)
        ensure_baseline(layout, original)
        blocks = _parked_blocks(layout, original)
        entry = _require_object(original, key, "entry")
        if len(original) <= 1:
            raise CatalogError("the catalog cannot be emptied")
        filament_id = _text_field(entry, "filament_id")
        if filament_id:
            _require_not_in_use(slots_in_use(layout), filament_id, key, "hide it")
        block = _stored_block(text, key)
        if block is None:
            raise CatalogError(f"could not locate the {key} block in the file text")
        bounds = _removal_bounds(text, key)
        if bounds is None:
            raise CatalogError(f"could not locate the {key} block in the file text")
        start, end = bounds
        updated_text = text[:start] + text[end:]
        updated = _parse_result(updated_text)
        if key in updated:
            raise CatalogError("entry is still present")
        ensure_untouched(original, updated, {key})
        if len(updated) != len(original) - 1:
            raise CatalogError("entry count did not decrease by one")
        if _entry_from_block(key, block) != entry:
            raise CatalogError("hidden material does not match the catalog entry")
        previous = _hidden_snapshot(layout)
        parked = dict(blocks)
        parked[key] = block
        _write_hidden_blocks(layout, parked)
        try:
            beside, config_copy = _commit(layout, updated_text, updated)
        except Exception:
            _restore_hidden(layout, previous)
            raise
        return ChangeResult(
            changed=True,
            message=f"Hid {key}.",
            beside=beside,
            config_copy=config_copy,
            before_count=len(original),
            after_count=len(updated),
        )


def unhide_filament(layout, name):
    """Splice a parked entry back onto the end of the catalog."""
    key = safe_token(name, "name")
    with _file_lock(layout):
        text, original = read_catalog(layout)
        ensure_baseline(layout, original)
        blocks = _load_hidden_blocks(layout)
        if key in original:
            if key in blocks:
                kept = {name: block for name, block in blocks.items() if name != key}
                _write_hidden_blocks(layout, kept)
            raise CatalogError(f"{key} is already in the catalog")
        blocks = _reconcile_hidden(layout, original, blocks)
        if key not in blocks:
            raise CatalogError(f"hidden material not found: {key}")
        block = blocks[key]
        entry = _entry_from_block(key, block)
        filament_id = _text_field(entry, "filament_id")
        if filament_id:
            others = _hidden_entries({name: item for name, item in blocks.items() if name != key})
            _ids_owned_by_others(original, filament_id, key, others)
        updated_text = _insert_at_end(text, original, block)
        updated = _parse_result(updated_text)
        if key not in updated:
            raise CatalogError("restored entry is missing")
        if updated.get(key) != entry:
            raise CatalogError("restored entry does not match the hidden material")
        ensure_untouched(original, updated, {key})
        if len(updated) != len(original) + 1:
            raise CatalogError("entry count did not increase by one")
        beside, config_copy = _commit(layout, updated_text, updated)
        kept = {name: item for name, item in blocks.items() if name != key}
        _write_hidden_blocks(layout, kept)
        return ChangeResult(
            changed=True,
            message=f"Unhid {key}.",
            beside=beside,
            config_copy=config_copy,
            before_count=len(original),
            after_count=len(updated),
        )


def delete_hidden_filament(layout, name):
    """Drop a parked entry. The live catalog is left as it is."""
    key = safe_token(name, "name")
    with _file_lock(layout):
        _text, original = read_catalog(layout)
        blocks = _parked_blocks(layout, original)
        if key not in blocks:
            raise CatalogError(f"hidden material not found: {key}")
        kept = {name: block for name, block in blocks.items() if name != key}
        _write_hidden_blocks(layout, kept)
        return ChangeResult(
            changed=True,
            message=f"Deleted the hidden copy of {key}. The load menu is unchanged.",
            before_count=len(original),
            after_count=len(original),
        )


def install_backup_name(layout):
    path = layout.state_dir / INSTALL_BACKUP_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or BACKUP_NAME.match(name) is None:
        return None
    return name


def ensure_install_backup(layout):
    """Back up the catalog once, on first install, and remember that backup's name."""
    existing = install_backup_name(layout)
    if existing is not None and any((folder / existing).is_file() for _where, folder in _backup_folders(layout)):
        return existing, False
    saved = create_backup(layout)
    name = saved.config_copy.name
    payload = json.dumps({"name": name}, indent=2) + "\n"
    try:
        layout.state_dir.mkdir(parents=True, exist_ok=True)
        (layout.state_dir / INSTALL_BACKUP_NAME).write_text(payload, encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"could not record the install backup: {exc}") from exc
    return name, True


def _catalog_filaments(layout):
    try:
        data = json.loads(_read_text(layout.catalog))
    except (CatalogError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {name: entry for name, entry in data.items() if isinstance(name, str) and isinstance(entry, dict)}


def _filaments_at(paths):
    for raw in paths:
        try:
            data = json.loads(_read_text(Path(raw)))
        except (CatalogError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        return {name: entry for name, entry in data.items() if isinstance(name, str) and isinstance(entry, dict)}
    return None


def _filament_title(name, entry):
    vendor = _text_field(entry, "filament_vendor")
    if vendor and name.startswith(vendor + " "):
        material = name[len(vendor) + 1:]
        if material:
            return f"{vendor} {material}"
    if vendor:
        return f"{vendor} {name}"
    return name


def _change_summary(before, after):
    """Describe filaments gained or lost between a backup and the later catalog."""
    if before is None or after is None:
        return "Unreadable"
    added = sorted(name for name in after if name not in before)
    removed = sorted(name for name in before if name not in after)
    if len(added) == 1 and not removed:
        return "Added " + _filament_title(added[0], after[added[0]])
    if len(removed) == 1 and not added:
        return "Removed " + _filament_title(removed[0], before[removed[0]])
    if added and not removed:
        return f"Added {len(added)} filaments"
    if removed and not added:
        return f"Removed {len(removed)} filaments"
    if added and removed:
        return f"+{len(added)}, -{len(removed)} filaments"
    return "No filament changes"


def _backup_folders(layout):
    return (
        ("beside", layout.catalog.parent),
        ("config", layout.config_dir),
    )


def list_backups(layout):
    protected = install_backup_name(layout)
    rows = {}
    for where, folder in _backup_folders(layout):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if not path.is_file() or not BACKUP_NAME.match(path.name):
                continue
            stat = path.stat()
            row = rows.get(path.name)
            if row is None:
                rows[path.name] = {
                    "name": path.name,
                    "locations": [where],
                    "paths": [str(path)],
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "install": path.name == protected,
                }
                continue
            row["locations"].append(where)
            row["paths"].append(str(path))
            if stat.st_mtime > row["modified"]:
                row["size"] = stat.st_size
                row["modified"] = stat.st_mtime
    ordered = list(rows.values())
    ordered.sort(key=lambda row: (row["modified"], row["name"]), reverse=True)
    current = _catalog_filaments(layout)
    parsed = [_filaments_at(row["paths"]) for row in ordered]
    for index, row in enumerate(ordered):
        later = current if index == 0 else parsed[index - 1]
        row["what"] = _change_summary(parsed[index], later)
    return ordered


def delete_backup(layout, name):
    if not isinstance(name, str) or BACKUP_NAME.match(name) is None:
        raise CatalogError("unknown backup")
    if name == install_backup_name(layout):
        raise CatalogError("This is the backup made when the app was installed. It cannot be deleted.")
    with _file_lock(layout):
        removed = 0
        for where, _folder in _backup_folders(layout):
            try:
                path = resolve_backup(layout, name, where)
            except CatalogError:
                continue
            try:
                path.unlink()
            except OSError as exc:
                raise CatalogError(f"could not delete the backup: {exc}") from exc
            removed += 1
        if not removed:
            raise CatalogError("backup not found")
    return ChangeResult(
        changed=True,
        message=f"Deleted {name}." if removed == 1 else f"Deleted both copies of {name}.",
    )


def resolve_backup(layout, name, where):
    if where not in ("beside", "config"):
        raise CatalogError("backup location must be beside or config")
    if not isinstance(name, str) or BACKUP_NAME.match(name) is None:
        raise CatalogError("unknown backup")
    root = layout.catalog.parent if where == "beside" else layout.config_dir
    path = (root / name).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise CatalogError("unknown backup") from exc
    if path.name != name or not path.is_file():
        raise CatalogError("backup not found")
    return path


def read_backup(layout, name, where):
    path = resolve_backup(layout, name, where)
    try:
        return path, path.read_bytes()
    except OSError as exc:
        raise CatalogError(f"could not read the backup: {exc}") from exc


def restore_backup(layout, name, where):
    with _file_lock(layout):
        source = resolve_backup(layout, name, where)
        try:
            raw = source.read_bytes()
        except OSError as exc:
            raise CatalogError(f"could not read the backup: {exc}") from exc
        try:
            text = raw.decode("utf-8")
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CatalogError("backup is not valid JSON") from exc
        if not isinstance(data, dict) or not data:
            raise CatalogError("backup does not contain a catalog")
        if any(not isinstance(value, dict) for value in data.values()):
            raise CatalogError("backup entries are not catalog objects")
        kept = _ids_in(data)
        for filament_id, numbers in sorted(slots_in_use(layout).items()):
            if filament_id not in kept:
                where = ", ".join(slot_label(number) for number in numbers)
                raise CatalogError(
                    f"This backup has no entry for {filament_id}, which is selected on {where}. "
                    "Klipper shuts down when it loads a slot whose id is not in the catalog. "
                    "Choose a material that is also in the backup for that slot on the touchscreen, then restore."
                )
        current = layout.catalog.read_bytes()
        if current == raw:
            return ChangeResult(
                changed=False,
                message="The catalog already matches this backup.",
                before_count=len(data),
                after_count=len(data),
            )
        try:
            before = len(json.loads(current.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            before = 0
        beside, config_copy = _commit(layout, text, data)
        return ChangeResult(
            changed=True,
            message=f"Restored {name}. The previous catalog was backed up first.",
            beside=beside,
            config_copy=config_copy,
            before_count=before,
            after_count=len(data),
        )
