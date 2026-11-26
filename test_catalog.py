#!/usr/bin/env python3
"""Stdlib checks for catalog edits and the local HTTP API."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog
import server


SAMPLE = """\
{
  "Alpha PLA": {
    "type": "PLA",
    "filament_id": "QFA01",
    "filament_vendor": "QIDI",
    "chamber_temperatures": "0",
    "nozzle_temperature": [190, 220],
    "note": "alpha } note"
  },
  "Beta Nylon": {
    "type": "NYLON",
    "filament_id": "QFN05",
    "filament_vendor": "QIDI",
    "chamber_temperatures": "55",
    "nozzle_temperature": [280, 300],
    "note": "beta-note"
  },
  "Gamma PETG": {
    "type": "PETG",
    "filament_id": "QFG10",
    "filament_vendor": "Generic",
    "chamber_temperatures": "45",
    "nozzle_temperature": [
      230,
      250
    ],
    "note": "gamma-note"
  }
}
"""

ODD = """\
{
  "Odd": {
    "type": "PLA",
    "type": "PLA",
    "filament_id": "QFA77",
    "filament_vendor": "QIDI",
    "chamber_temperatures": "0",
    "nozzle_temperature": [190, 210],
    "note": "odd-note"
  },
  "Other": {
    "type": "PETG",
    "filament_id": "QFG11",
    "filament_vendor": "Generic",
    "chamber_temperatures": "40",
    "nozzle_temperature": [230, 250],
    "note": "other-note"
  }
}
"""


def parsed(layout):
    text, data = catalog.read_working(layout)
    return text, data


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make_home(self, user, text=SAMPLE):
        home = Path(self.tmp.name) / user
        catalog_path = home / catalog.CATALOG_RELATIVE
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_bytes(text.encode("utf-8"))
        return home

    def layout(self, text=SAMPLE):
        home = self.make_home("qidi", text)
        found = catalog.discover([home])
        self.assertIsNotNone(found)
        return found

    def test_discover_prefers_the_first_home_and_falls_through(self):
        qidi = self.make_home("qidi", SAMPLE.replace("alpha } note", "from-qidi"))
        mks = self.make_home("mks", SAMPLE.replace("alpha } note", "from-mks"))
        self.assertEqual(catalog.discover([qidi, mks]).home, qidi)
        self.assertEqual(catalog.discover([mks]).home, mks)
        self.assertIsNone(catalog.discover([Path(self.tmp.name) / "missing"]))
        self.assertEqual(
            [path.name for path in catalog.default_homes()],
            ["qidi", "mks"],
        )
        message = catalog.missing_catalog_message()
        self.assertIn("does not use that catalog", message)
        self.assertIn("/home/qidi/", message.replace("\\", "/"))
        self.assertIn("/home/mks/", message.replace("\\", "/"))

    def test_add_copies_template_and_leaves_other_entries(self):
        layout = self.layout()
        before_text, before = parsed(layout)
        result = catalog.add_filament(
            layout,
            "Beta Nylon",
            "Generic Delta Nylon",
            "NYLON",
            "QFN90",
            "Generic",
        )
        self.assertTrue(result.changed)
        self.assertEqual(result.before_count, 3)
        self.assertEqual(result.after_count, 4)
        text, data = parsed(layout)
        self.assertEqual(data["Alpha PLA"], before["Alpha PLA"])
        self.assertEqual(data["Gamma PETG"], before["Gamma PETG"])
        self.assertEqual(data["Beta Nylon"], before["Beta Nylon"])
        self.assertEqual(data["Generic Delta Nylon"]["note"], "beta-note")
        self.assertEqual(data["Generic Delta Nylon"]["filament_id"], "QFN90")
        self.assertEqual(data["Generic Delta Nylon"]["filament_vendor"], "Generic")
        self.assertEqual(text.count("beta-note"), 2)
        self.assertEqual(text.count("alpha } note"), 1)
        self.assertEqual(before_text.count("alpha } note"), 1)
        self.assertIsNone(result.beside)
        self.assertEqual(layout.catalog.read_bytes(), before_text.encode("utf-8"))
        self.assertEqual(list(layout.catalog.parent.glob("filament-0.4.json.bak-*")), [])
        applied = catalog.apply_staged(layout)
        self.assertTrue(applied.changed)
        self.assertEqual(applied.beside.read_bytes(), before_text.encode("utf-8"))
        self.assertEqual(applied.config_copy.read_bytes(), before_text.encode("utf-8"))
        self.assertEqual(layout.catalog.read_bytes(), text.encode("utf-8"))
        self.assertFalse(catalog.has_staged_changes(layout))

    def test_add_from_the_last_entry_stays_valid_json(self):
        layout = self.layout()
        _, before = parsed(layout)
        catalog.add_filament(layout, "Gamma PETG", "Generic Gamma Copy", "PETG-HF", "QFG90", "Generic", nozzle=(232, 255), chamber="50")
        text, data = parsed(layout)
        self.assertEqual(data["Alpha PLA"], before["Alpha PLA"])
        self.assertEqual(data["Beta Nylon"], before["Beta Nylon"])
        self.assertEqual(data["Generic Gamma Copy"]["nozzle_temperature"], [232, 255])
        self.assertEqual(data["Generic Gamma Copy"]["chamber_temperatures"], "50")
        self.assertEqual(data["Generic Gamma Copy"]["note"], "gamma-note")
        self.assertNotRegex(text, r",\s*\}")

    def test_duplicate_id_and_broken_template_do_not_write(self):
        layout = self.layout()
        original = layout.catalog.read_bytes()
        with self.assertRaises(catalog.CatalogError):
            catalog.add_filament(layout, "Alpha PLA", "Generic Other", "PLA", "QFA01", "Generic")
        self.assertEqual(layout.catalog.read_bytes(), original)
        self.assertEqual(list(layout.catalog.parent.glob("filament-0.4.json.bak-*")), [])

        odd = self.layout(ODD)
        odd_bytes = odd.catalog.read_bytes()
        with self.assertRaises(catalog.CatalogError):
            catalog.add_filament(odd, "Odd", "Generic Odd Copy", "PLA-HF", "QFA78", "Generic")
        self.assertEqual(odd.catalog.read_bytes(), odd_bytes)
        self.assertFalse(odd.config_dir.exists() and any(odd.config_dir.glob("filament-0.4.json.bak-*")))

    def test_name_must_match_vendor(self):
        layout = self.layout()
        original = layout.catalog.read_bytes()
        with self.assertRaises(catalog.CatalogError) as mismatched:
            catalog.add_filament(layout, "Alpha PLA", "Bar PETG", "PETG", "QFA91", "Foo")
        self.assertIn("load list", str(mismatched.exception))
        with self.assertRaises(catalog.CatalogError) as spaced:
            catalog.add_filament(layout, "Alpha PLA", "Foo Bar PETG", "PETG", "QFA91", "Foo Bar")
        self.assertIn("single word", str(spaced.exception))
        with self.assertRaises(catalog.CatalogError):
            catalog.update_filament(layout, "Alpha PLA", "Bar PETG", "PLA", "QFA01", "Foo")
        self.assertEqual(layout.catalog.read_bytes(), original)
        catalog.add_filament(layout, "Alpha PLA", "Foo PETG", "PETG", "QFA91", "Foo")
        _, data = parsed(layout)
        self.assertEqual(data["Foo PETG"]["filament_vendor"], "Foo")

    def test_idempotent_add_does_not_back_up_again(self):
        layout = self.layout()
        catalog.add_filament(layout, "Alpha PLA", "Generic Alpha Copy", "PLA", "QFA90", "Generic")
        backups = catalog.list_backups(layout)
        again = catalog.add_filament(layout, "Alpha PLA", "Generic Alpha Copy", "PLA", "QFA90", "Generic")
        self.assertFalse(again.changed)
        self.assertIn("already present", again.message)
        self.assertEqual(catalog.list_backups(layout), backups)

    def test_update_rename_and_delete_positions(self):
        layout = self.layout()
        _, before = parsed(layout)
        renamed = catalog.update_filament(
            layout,
            "Beta Nylon",
            "Generic Beta HF",
            "NYLON-HF",
            "QFN90",
            "Generic",
            nozzle=(270, 290),
            chamber="60",
        )
        self.assertTrue(renamed.changed)
        _, data = parsed(layout)
        self.assertNotIn("Beta Nylon", data)
        self.assertEqual(data["Alpha PLA"], before["Alpha PLA"])
        self.assertEqual(data["Gamma PETG"], before["Gamma PETG"])
        self.assertEqual(data["Generic Beta HF"]["note"], "beta-note")
        self.assertEqual(data["Generic Beta HF"]["nozzle_temperature"], [270, 290])
        self.assertEqual(data["Generic Beta HF"]["chamber_temperatures"], "60")
        self.assertEqual(data["Generic Beta HF"]["filament_id"], "QFN90")

        unchanged = catalog.update_filament(
            layout,
            "Generic Beta HF",
            "Generic Beta HF",
            "NYLON-HF",
            "QFN90",
            "Generic",
            nozzle=(270, 290),
            chamber="60",
        )
        self.assertFalse(unchanged.changed)

        with self.assertRaises(catalog.CatalogError):
            catalog.update_filament(layout, "Generic Beta HF", "Gamma PETG", "NYLON-HF", "QFN90", "Generic")

        catalog.delete_filament(layout, "Alpha PLA")
        text, data = parsed(layout)
        self.assertNotIn("Alpha PLA", data)
        self.assertNotIn("alpha } note", text)
        self.assertNotIn('"Alpha PLA"', text)
        self.assertEqual(data["Gamma PETG"]["note"], "gamma-note")
        self.assertEqual(data["Generic Beta HF"]["note"], "beta-note")
        json.loads(text)

        catalog.delete_filament(layout, "Gamma PETG")
        text, data = parsed(layout)
        self.assertEqual(list(data), ["Generic Beta HF"])
        self.assertNotRegex(text, r",\s*\}")
        with self.assertRaises(catalog.CatalogError) as ctx:
            catalog.delete_filament(layout, "Generic Beta HF")
        self.assertIn("cannot be emptied", str(ctx.exception))
        self.assertEqual(list(parsed(layout)[1]), ["Generic Beta HF"])

    def test_delete_middle_entry(self):
        layout = self.layout()
        _, before = parsed(layout)
        catalog.delete_filament(layout, "Beta Nylon")
        text, data = parsed(layout)
        self.assertEqual(set(data), {"Alpha PLA", "Gamma PETG"})
        self.assertEqual(data["Alpha PLA"], before["Alpha PLA"])
        self.assertEqual(data["Gamma PETG"], before["Gamma PETG"])
        self.assertNotIn("beta-note", text)
        self.assertNotRegex(text, r",\s*\}")

    def test_baseline_marks_only_later_ids(self):
        layout = self.layout()
        rows = catalog.filament_rows(layout)
        self.assertTrue(all(not row["added_here"] for row in rows["filaments"]))
        baseline = layout.state_dir / catalog.BASELINE_NAME
        recorded = baseline.read_bytes()
        catalog.add_filament(layout, "Beta Nylon", "Generic Delta Nylon", "NYLON", "QFN90", "Generic")
        rows = catalog.filament_rows(layout)
        added = [row["id"] for row in rows["filaments"] if row["added_here"]]
        self.assertEqual(added, ["QFN90"])
        self.assertEqual(baseline.read_bytes(), recorded)

    def test_backup_restore_and_rejected_names(self):
        layout = self.layout()
        original = layout.catalog.read_bytes()
        saved = catalog.create_backup(layout)
        self.assertEqual(layout.catalog.read_bytes(), original)
        self.assertEqual(saved.beside.read_bytes(), original)
        self.assertEqual(saved.config_copy.read_bytes(), original)
        catalog.delete_filament(layout, "Gamma PETG")
        self.assertIn(b"gamma-note", layout.catalog.read_bytes())
        catalog.apply_staged(layout)
        self.assertNotIn(b"gamma-note", layout.catalog.read_bytes())
        listed = catalog.list_backups(layout)
        manual = next(row for row in listed if row["name"] == saved.config_copy.name)
        self.assertEqual(manual["locations"], ["beside", "config"])
        self.assertEqual(len(manual["paths"]), 2)
        self.assertTrue(all(manual["name"] in path for path in manual["paths"]))
        self.assertEqual(manual["what"], "No filament changes")
        self.assertEqual(len([row for row in listed if row["name"] == manual["name"]]), 1)
        restored = catalog.restore_backup(layout, manual["name"], "config")
        self.assertTrue(restored.changed)
        self.assertNotEqual(layout.catalog.read_bytes(), original)
        catalog.apply_staged(layout)
        self.assertEqual(layout.catalog.read_bytes(), original)
        same = catalog.restore_backup(layout, manual["name"], "config")
        self.assertFalse(same.changed)
        with self.assertRaises(catalog.CatalogError):
            catalog.resolve_backup(layout, "../filament-0.4.json.bak-20260101_000000", "config")
        with self.assertRaises(catalog.CatalogError):
            catalog.resolve_backup(layout, "filament-0.4.json", "beside")
        with self.assertRaises(catalog.CatalogError):
            catalog.add_filament(layout, "Alpha PLA", 'Bad "Name', "PLA", "QFA91", "Generic")

    def test_delete_backup_removes_both_copies(self):
        layout = self.layout()
        saved = catalog.create_backup(layout)
        name = saved.config_copy.name
        deleted = catalog.delete_backup(layout, name)
        self.assertTrue(deleted.changed)
        self.assertFalse(saved.beside.exists())
        self.assertFalse(saved.config_copy.exists())
        self.assertNotIn(name, [row["name"] for row in catalog.list_backups(layout)])
        with self.assertRaises(catalog.CatalogError):
            catalog.delete_backup(layout, name)
        with self.assertRaises(catalog.CatalogError):
            catalog.delete_backup(layout, "../filament-0.4.json.bak-20260101_000000")

        orphan = catalog.create_backup(layout)
        orphan.beside.unlink()
        listed = next(row for row in catalog.list_backups(layout) if row["name"] == orphan.config_copy.name)
        self.assertEqual(listed["locations"], ["config"])
        self.assertEqual(len(listed["paths"]), 1)
        self.assertTrue(listed["paths"][0].endswith(orphan.config_copy.name))
        catalog.delete_backup(layout, orphan.config_copy.name)
        self.assertFalse(orphan.config_copy.exists())

    def test_install_backup_is_made_once_and_kept(self):
        layout = self.layout()
        name, created = catalog.ensure_install_backup(layout)
        self.assertTrue(created)
        again, created_again = catalog.ensure_install_backup(layout)
        self.assertEqual(again, name)
        self.assertFalse(created_again)
        self.assertEqual(len([row for row in catalog.list_backups(layout) if row["install"]]), 1)
        with self.assertRaises(catalog.CatalogError):
            catalog.delete_backup(layout, name)
        self.assertTrue((layout.config_dir / name).is_file())

        (layout.config_dir / name).unlink()
        (layout.catalog.parent / name).unlink()
        replaced, created_replacement = catalog.ensure_install_backup(layout)
        self.assertTrue(created_replacement)
        self.assertTrue((layout.config_dir / replaced).is_file())
        self.assertTrue((layout.catalog.parent / replaced).is_file())
        self.assertEqual(catalog.install_backup_name(layout), replaced)

    def test_backup_what_describes_the_following_change(self):
        alpha = {"filament_vendor": "QIDI"}
        beta = {"filament_vendor": "QIDI"}
        gamma = {"filament_vendor": "Generic"}
        self.assertEqual(catalog._change_summary({"Alpha PLA": alpha}, {"Alpha PLA": alpha}), "No filament changes")
        self.assertEqual(catalog._change_summary({}, {"QIDI PLA": alpha}), "Added QIDI PLA")
        self.assertEqual(
            catalog._change_summary({"Alpha PLA": alpha}, {"Alpha PLA": alpha, "Beta Nylon": beta}),
            "Added QIDI Beta Nylon",
        )
        self.assertEqual(
            catalog._change_summary({"Gamma PETG": gamma, "Alpha PLA": alpha}, {"Alpha PLA": alpha}),
            "Removed Generic Gamma PETG",
        )
        self.assertEqual(
            catalog._change_summary(
                {"Gamma PETG": gamma, "Alpha PLA": alpha},
                {"Alpha PLA": alpha},
                hidden_names={"Gamma PETG"},
            ),
            "Hid Generic Gamma PETG",
        )
        hidden_sixteen = {f"Filament {index}": alpha for index in range(16)}
        hidden_sixteen["Keep"] = alpha
        self.assertEqual(
            catalog._change_summary(hidden_sixteen, {"Keep": alpha}, hidden_names=set(hidden_sixteen) - {"Keep"}),
            "Hid 16 filaments",
        )
        self.assertEqual(
            catalog._change_summary({"A": alpha}, {"A": alpha, "B": beta, "C": gamma}),
            "Added 2 filaments",
        )
        self.assertEqual(
            catalog._change_summary({"A": alpha, "B": beta, "C": gamma}, {"A": alpha}),
            "Removed 2 filaments",
        )
        self.assertEqual(
            catalog._change_summary(
                {"A": alpha, "B": beta},
                {"A": alpha, "C": gamma, "D": alpha, "E": alpha},
            ),
            "+3, -1 filaments",
        )
        self.assertEqual(catalog._change_summary(None, {"A": alpha}), "Unreadable")

        layout = self.layout()
        catalog.delete_filament(layout, "Gamma PETG")
        catalog.apply_staged(layout)
        newest = catalog.list_backups(layout)[0]
        self.assertEqual(newest["what"], "Removed Generic Gamma PETG")

        hidden = self.layout()
        catalog.hide_filament(hidden, "Gamma PETG")
        catalog.apply_staged(hidden)
        self.assertEqual(catalog.list_backups(hidden)[0]["what"], "Hid Generic Gamma PETG")

    def write_saved_variables(self, layout, external="'QFN05'"):
        layout.config_dir.mkdir(parents=True, exist_ok=True)
        (layout.config_dir / catalog.SAVED_VARIABLES_NAME).write_text(
            "[Variables]\n"
            "color_slot16 = ['228332']\n"
            "filament_slot0 = 1\n"
            "filament_slot5 = 'QFG10'\n"
            f"filament_slot16 = {external}\n"
            "last_load_slot = 'slot16'\n",
            encoding="utf-8",
        )

    def test_slots_in_use_reads_string_ids_only(self):
        layout = self.layout()
        self.assertEqual(catalog.slots_in_use(layout), {})
        self.write_saved_variables(layout)
        self.assertEqual(catalog.slots_in_use(layout), {"QFN05": [16], "QFG10": [5]})
        self.assertEqual(catalog.slot_label(16), "the external spool")
        self.assertEqual(catalog.slot_label(5), "box 2 slot 2")
        rows = {row["name"]: row for row in catalog.filament_rows(layout)["filaments"]}
        self.assertEqual(rows["Beta Nylon"]["in_use"], ["the external spool"])
        self.assertEqual(rows["Alpha PLA"]["in_use"], [])

    def test_in_use_ids_cannot_be_removed_or_renumbered(self):
        layout = self.layout()
        self.write_saved_variables(layout)
        original = layout.catalog.read_bytes()
        with self.assertRaises(catalog.CatalogError) as removed:
            catalog.delete_filament(layout, "Beta Nylon")
        self.assertIn("external spool", str(removed.exception))
        with self.assertRaises(catalog.CatalogError) as renumbered:
            catalog.update_filament(layout, "Beta Nylon", "QIDI Beta Nylon", "NYLON", "QFN99", "QIDI")
        self.assertIn("change its id", str(renumbered.exception))
        self.assertEqual(layout.catalog.read_bytes(), original)

        renamed = catalog.update_filament(layout, "Beta Nylon", "QIDI Beta Renamed", "NYLON", "QFN05", "QIDI")
        self.assertTrue(renamed.changed)
        catalog.delete_filament(layout, "Alpha PLA")

    def test_restore_keeps_ids_that_slots_use(self):
        layout = self.layout()
        saved = catalog.create_backup(layout)
        catalog.add_filament(layout, "Alpha PLA", "Generic Alpha Copy", "PLA", "QFA90", "Generic")
        self.write_saved_variables(layout, external="'QFA90'")
        with self.assertRaises(catalog.CatalogError) as ctx:
            catalog.restore_backup(layout, saved.config_copy.name, "config")
        self.assertIn("QFA90", str(ctx.exception))
        self.assertIn("Generic Alpha Copy", parsed(layout)[1])
        self.write_saved_variables(layout)
        catalog.apply_staged(layout)
        self.assertTrue(catalog.restore_backup(layout, saved.config_copy.name, "config").changed)

    def test_untouched_guard_names_the_entry(self):
        original = {"A": {"filament_id": "1"}, "B": {"filament_id": "2"}}
        updated = {"A": {"filament_id": "1"}, "B": {"filament_id": "9"}}
        with self.assertRaises(catalog.CatalogError) as ctx:
            catalog.ensure_untouched(original, updated, {"A"})
        self.assertIn("B", str(ctx.exception))

    def hidden_blocks(self, layout):
        return json.loads((layout.state_dir / catalog.HIDDEN_NAME).read_text(encoding="utf-8"))

    def test_hide_preserves_neighbors_and_unhide_restores_text(self):
        layout = self.layout()
        original = layout.catalog.read_bytes()
        before_text, before = parsed(layout)
        result = catalog.hide_filament(layout, "Beta Nylon")
        self.assertTrue(result.changed)
        self.assertEqual(result.before_count, 3)
        self.assertEqual(result.after_count, 2)
        self.assertIsNone(result.beside)
        self.assertEqual(layout.catalog.read_bytes(), original)
        self.assertEqual(list(layout.catalog.parent.glob("filament-0.4.json.bak-*")), [])
        text, data = parsed(layout)
        self.assertNotIn("Beta Nylon", data)
        self.assertEqual(data["Alpha PLA"], before["Alpha PLA"])
        self.assertEqual(data["Gamma PETG"], before["Gamma PETG"])
        self.assertIn("alpha } note", text)
        self.assertIn("gamma-note", text)
        self.assertNotIn("beta-note", text)
        self.assertNotRegex(text, r",\s*\}")
        start, end = catalog.object_span(before_text, "Beta Nylon")
        expected = before_text[start:end]
        if expected.endswith(","):
            expected = expected[:-1]
        self.assertEqual(self.hidden_blocks(layout)["Beta Nylon"], expected)
        listed = catalog.filament_rows(layout)
        self.assertEqual(listed["count"], 2)
        hidden_rows = [row for row in listed["filaments"] if row["hidden"]]
        self.assertEqual([row["name"] for row in hidden_rows], ["Beta Nylon"])
        self.assertEqual(hidden_rows[0]["id"], "QFN05")
        self.assertTrue(all(not row["hidden"] for row in listed["filaments"] if row["name"] != "Beta Nylon"))

        catalog.unhide_filament(layout, "Beta Nylon")
        self.assertEqual(parsed(layout)[1], before)
        self.assertEqual(self.hidden_blocks(layout), {})

        fresh = self.layout()
        fresh_bytes = fresh.catalog.read_bytes()
        catalog.hide_filament(fresh, "Gamma PETG")
        catalog.unhide_filament(fresh, "Gamma PETG")
        self.assertEqual(fresh.catalog.read_bytes(), fresh_bytes)
        self.assertEqual(self.hidden_blocks(fresh), {})

    def test_hide_refuses_the_last_entry_and_a_slot_in_use(self):
        layout = self.layout()
        catalog.hide_filament(layout, "Alpha PLA")
        catalog.hide_filament(layout, "Beta Nylon")
        with self.assertRaises(catalog.CatalogError) as emptied:
            catalog.hide_filament(layout, "Gamma PETG")
        self.assertIn("cannot be emptied", str(emptied.exception))
        self.assertIn("Gamma PETG", parsed(layout)[1])
        self.assertNotIn("Gamma PETG", self.hidden_blocks(layout))

        busy_home = self.make_home("mks")
        busy = catalog.discover([busy_home])
        self.write_saved_variables(busy)
        before = busy.catalog.read_bytes()
        with self.assertRaises(catalog.CatalogError) as hidden:
            catalog.hide_filament(busy, "Beta Nylon")
        self.assertIn("hide it", str(hidden.exception))
        self.assertIn("external spool", str(hidden.exception))
        self.assertEqual(busy.catalog.read_bytes(), before)
        self.assertFalse((busy.state_dir / catalog.HIDDEN_NAME).is_file())

    def test_hidden_ids_and_names_block_add_update_and_unhide(self):
        layout = self.layout()
        catalog.hide_filament(layout, "Beta Nylon")
        with self.assertRaises(catalog.CatalogError) as added:
            catalog.add_filament(layout, "Alpha PLA", "Generic Beta Copy", "NYLON", "QFN05", "Generic")
        self.assertIn("QFN05", str(added.exception))
        self.assertIn("hidden", str(added.exception))
        with self.assertRaises(catalog.CatalogError) as renamed:
            catalog.update_filament(layout, "Alpha PLA", "QIDI Alpha PLA", "PLA", "QFN05", "QIDI")
        self.assertIn("hidden", str(renamed.exception))
        with self.assertRaises(catalog.CatalogError) as same_name:
            catalog.add_filament(layout, "Alpha PLA", "Beta Nylon", "NYLON", "QFN90", "Beta")
        self.assertIn("Unhide it", str(same_name.exception))
        with self.assertRaises(catalog.CatalogError) as taken_name:
            catalog.update_filament(layout, "Alpha PLA", "Beta Nylon", "PLA", "QFA01", "Beta")
        self.assertIn("Unhide it", str(taken_name.exception))
        self.assertNotIn("Generic Beta Copy", parsed(layout)[1])

        copied = catalog.add_filament(layout, "Beta Nylon", "Generic Beta Copy", "NYLON", "QFN90", "Generic")
        self.assertTrue(copied.changed)
        data = parsed(layout)[1]
        self.assertEqual(data["Generic Beta Copy"]["note"], "beta-note")
        self.assertEqual(data["Generic Beta Copy"]["filament_id"], "QFN90")
        self.assertNotIn("Beta Nylon", data)
        rows = {row["name"]: row for row in catalog.filament_rows(layout)["filaments"]}
        self.assertTrue(rows["Beta Nylon"]["hidden"])

        catalog.apply_staged(layout)
        text = layout.catalog.read_text(encoding="utf-8")
        layout.catalog.write_text(text.replace('"filament_id": "QFA01"', '"filament_id": "QFN05"'), encoding="utf-8")
        with self.assertRaises(catalog.CatalogError) as unhide:
            catalog.unhide_filament(layout, "Beta Nylon")
        self.assertIn("QFN05", str(unhide.exception))
        self.assertNotIn("Beta Nylon", parsed(layout)[1])
        self.assertIn("Beta Nylon", self.hidden_blocks(layout))

    def test_catalog_wins_when_a_hidden_name_returns(self):
        layout = self.layout()
        original = layout.catalog.read_bytes()
        catalog.hide_filament(layout, "Gamma PETG")
        catalog.hide_filament(layout, "Beta Nylon")
        catalog.apply_staged(layout)
        layout.catalog.write_bytes(original)
        listed = catalog.filament_rows(layout)
        names = [row["name"] for row in listed["filaments"]]
        self.assertEqual(names.count("Gamma PETG"), 1)
        self.assertEqual(names.count("Beta Nylon"), 1)
        self.assertTrue(all(not row["hidden"] for row in listed["filaments"]))
        self.assertEqual(self.hidden_blocks(layout), {})
        self.assertEqual(layout.catalog.read_bytes(), original)

        catalog.hide_filament(layout, "Gamma PETG")
        catalog.apply_staged(layout)
        layout.catalog.write_bytes(original)
        with self.assertRaises(catalog.CatalogError) as ctx:
            catalog.unhide_filament(layout, "Gamma PETG")
        self.assertIn("already in the catalog", str(ctx.exception))
        self.assertIn("Gamma PETG", parsed(layout)[1])
        self.assertNotIn("Gamma PETG", self.hidden_blocks(layout))

    def test_delete_hidden_leaves_the_catalog(self):
        layout = self.layout()
        catalog.hide_filament(layout, "Gamma PETG")
        catalog_bytes = layout.catalog.read_bytes()
        result = catalog.delete_hidden_filament(layout, "Gamma PETG")
        self.assertTrue(result.changed)
        self.assertIn("unchanged", result.message)
        self.assertEqual(layout.catalog.read_bytes(), catalog_bytes)
        self.assertEqual(result.beside, None)
        with self.assertRaises(catalog.CatalogError) as ctx:
            catalog.unhide_filament(layout, "Gamma PETG")
        self.assertIn("not found", str(ctx.exception))
        self.assertEqual(layout.catalog.read_bytes(), catalog_bytes)

    def test_failed_apply_leaves_the_staged_catalog(self):
        layout = self.layout()
        catalog.hide_filament(layout, "Alpha PLA")
        catalog.hide_filament(layout, "Beta Nylon")
        parked = (layout.state_dir / catalog.HIDDEN_NAME).read_bytes()
        catalog_bytes = layout.catalog.read_bytes()
        original_commit = catalog._commit

        def boom(layout, updated_text, updated):
            raise catalog.CatalogError("disk full")

        catalog._commit = boom
        try:
            with self.assertRaises(catalog.CatalogError) as ctx:
                catalog.apply_staged(layout)
            self.assertIn("disk full", str(ctx.exception))
        finally:
            catalog._commit = original_commit
        self.assertEqual(layout.catalog.read_bytes(), catalog_bytes)
        self.assertEqual((layout.state_dir / catalog.HIDDEN_NAME).read_bytes(), parked)
        self.assertTrue(catalog.has_staged_changes(layout))
        self.assertNotIn("Beta Nylon", parsed(layout)[1])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / "qidi"
        catalog_path = home / catalog.CATALOG_RELATIVE
        catalog_path.parent.mkdir(parents=True)
        catalog_path.write_bytes(SAMPLE.encode("utf-8"))
        self.home = home

    def test_primary_color_from_fluidd_theme(self):
        self.assertEqual(server.primary_from_theme({"color": "#9d00eb", "isDark": True}), "#9d00eb")
        self.assertEqual(server.primary_from_theme({"color": "  #ABC "}), "#ABC")
        self.assertEqual(server.primary_from_theme({"color": "red"}), server.DEFAULT_PRIMARY)
        self.assertEqual(server.primary_from_theme(None), server.DEFAULT_PRIMARY)
        self.assertEqual(server.primary_from_theme({"color": "#12"}), server.DEFAULT_PRIMARY)

    def test_port_and_stale_flag(self):
        self.assertEqual(server.listen_port("8091"), 8091)
        with self.assertRaises(ValueError):
            server.listen_port("0")
        with self.assertRaises(ValueError):
            server.listen_port("nope")
        path = self.home / catalog.CATALOG_RELATIVE
        os.utime(path, (1_000, 1_000))
        self.assertIsNone(server.catalog_changed_since_start(path, None))
        self.assertFalse(server.catalog_changed_since_start(path, 2_000))
        self.assertTrue(server.catalog_changed_since_start(path, 100))

    def test_print_state_reads_moonraker_shape(self):
        class Quiet(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"result": {"status": {"print_stats": {"state": "standby"}}}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        def stop():
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        port = httpd.server_address[1]
        state = server.read_print_state(f"http://127.0.0.1:{port}/printer/objects/query?print_stats")
        self.assertEqual(state, "standby")

    def test_monotonic_stamp_ignores_boot_clock(self):
        started = server.monotonic_to_wall("8262489", now_wall=10_000.0, now_monotonic=100.0)
        self.assertAlmostEqual(started, 10_000.0 - (100.0 - 8.262489))
        self.assertIsNone(server.monotonic_to_wall("0"))
        self.assertIsNone(server.monotonic_to_wall("n/a"))

    def test_applied_record_only_counts_for_this_boot(self):
        state_dir = self.home / "filament-manager" / "state"
        self.assertIsNone(server.read_applied(state_dir, "boot-a"))
        server.record_applied(state_dir, "boot-a", 1234.5)
        self.assertEqual(server.read_applied(state_dir, "boot-a"), 1234.5)
        self.assertIsNone(server.read_applied(state_dir, "boot-b"))
        self.assertIsNone(server.read_applied(state_dir, None))

    def test_idle_and_apply_rules(self):
        self.assertIsNone(server.idle_block("standby", None))
        self.assertIsNone(server.idle_block("complete", None))
        self.assertEqual(server.idle_block("printing", None), server.PRINTER_BUSY)
        self.assertEqual(server.idle_block("paused", None), server.PRINTER_BUSY)
        self.assertEqual(server.idle_block(None, "no moonraker"), "no moonraker")
        self.assertIsNone(server.apply_block("printing", None, "shutdown"))
        self.assertIsNone(server.apply_block(None, "no print state", "error"))
        self.assertEqual(server.apply_block("printing", None, "ready"), server.PRINTER_BUSY)

    def test_wait_ignores_the_state_from_before_the_restart(self):
        def run(states):
            sequence = list(states)
            now = [0.0]

            def fake_read(url):
                state = sequence.pop(0) if sequence else "ready"
                if state is None:
                    raise server.ActionError("down")
                return state, f"{state} message"

            original = server.read_klippy_state
            server.read_klippy_state = fake_read
            try:
                return server.wait_for_klipper(
                    "unused",
                    timeout=30,
                    sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                    clock=lambda: now[0],
                )
            finally:
                server.read_klippy_state = original

        self.assertEqual(run(["shutdown", None, "startup", "ready"])[0], "ready")
        self.assertEqual(run(["ready", "startup", "shutdown"])[0], "shutdown")
        self.assertEqual(run(["ready"] * 40)[0], "ready")
        with self.assertRaises(server.ActionError):
            run(["startup"] * 40)

    def fake_moonraker(self, klippy_state, print_state):
        calls = []

        class Fake(BaseHTTPRequestHandler):
            def reply(self, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/printer/info"):
                    self.reply({"result": {"state": klippy_state, "state_message": "msg"}})
                else:
                    self.reply({"result": {"status": {"print_stats": {"state": print_state}}}})

            def do_POST(self):
                calls.append(self.path)
                self.reply({"result": "ok"})

            def log_message(self, fmt, *args):
                return

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        def stop():
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        return base, calls

    def test_apply_restarts_klipper_and_records_it(self):
        if server.client_state()["unit_installed"]:
            self.skipTest("qidi-client is installed here")
        layout = catalog.discover([self.home])
        base, calls = self.fake_moonraker("ready", "printing")
        urls = {
            "info_url": base + "/printer/info",
            "restart_url": base + "/printer/firmware_restart",
            "print_url": base + "/printer/objects/query?print_stats",
        }
        with self.assertRaises(server.ActionError) as busy:
            server.apply_changes(layout, wait=lambda url: ("ready", ""), **urls)
        self.assertEqual(str(busy.exception), server.PRINTER_BUSY)
        self.assertEqual(calls, [])

        base, calls = self.fake_moonraker("shutdown", "error")
        urls = {
            "info_url": base + "/printer/info",
            "restart_url": base + "/printer/firmware_restart",
            "print_url": base + "/printer/objects/query?print_stats",
        }
        with self.assertRaises(server.ActionError) as still_down:
            server.apply_changes(layout, wait=lambda url: ("shutdown", "boom"), **urls)
        self.assertIn("shutdown", str(still_down.exception))
        self.assertEqual(calls, ["/printer/firmware_restart"])

        original_boot = server.boot_id
        server.boot_id = lambda path=None: "boot-a"
        try:
            result = server.apply_changes(layout, wait=lambda url: ("ready", ""), **urls)
            self.assertTrue(result["ok"])
            self.assertIsNotNone(server.read_applied(layout.state_dir, "boot-a"))
            os.utime(layout.catalog, (1_000, 1_000))
            self.assertFalse(server._klipper_stale(layout))
        finally:
            server.boot_id = original_boot

    def test_http_round_trip(self):
        if server.client_state()["unit_installed"]:
            self.skipTest("qidi-client is installed here")
        httpd = server.create_server("127.0.0.1", 0, homes=[self.home])
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        def stop():
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        port = httpd.server_address[1]

        def call(path, payload=None, content_type="application/json"):
            data = None if payload is None else json.dumps(payload).encode("utf-8")
            headers = {}
            if payload is not None:
                headers["Content-Type"] = content_type
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=data,
                headers=headers,
                method="GET" if payload is None else "POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

        status, page = call("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Filament Manager", page)
        self.assertIn(b"Reboot printer", page)
        self.assertIn(b"Apply Changes", page)
        self.assertIn(b"Show hidden", page)
        self.assertIn(b"Unhide", page)
        self.assertIn(b"The whole printer reboots, including Klipper.", page)
        self.assertIn(b"add-material", page)

        status, body = call("/api/status")
        self.assertEqual(status, 200)
        status_payload = json.loads(body)
        self.assertTrue(status_payload["found"])
        self.assertFalse(status_payload["restart_allowed"])
        self.assertFalse(status_payload["reboot_allowed"])
        self.assertFalse(status_payload["apply_allowed"])
        self.assertTrue(str(status_payload["primary"]).startswith("#"))

        status, body = call("/api/filaments")
        filaments = json.loads(body)
        self.assertEqual(filaments["count"], 3)
        self.assertTrue(all(not row["added_here"] for row in filaments["filaments"]))

        status, body = call("/api/filaments", {
            "template": "Beta Nylon",
            "name": "Generic Delta Nylon",
            "type": "NYLON",
            "id": "QFN90",
            "vendor": "Generic",
        })
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["changed"])

        status, body = call("/api/filaments")
        rows = json.loads(body)["filaments"]
        added = [row for row in rows if row["added_here"]]
        self.assertEqual([row["name"] for row in added], ["Generic Delta Nylon"])

        status, body = call("/api/filaments/update", {
            "original": "Generic Delta Nylon",
            "name": "Generic Delta HF",
            "type": "NYLON-HF",
            "id": "QFN91",
            "vendor": "Generic",
            "nozzle_min": 275,
            "nozzle_max": 295,
            "chamber": "58",
        })
        self.assertEqual(status, 200, body)
        _, data = parsed(catalog.discover([self.home]))
        self.assertEqual(data["Generic Delta HF"]["nozzle_temperature"], [275, 295])
        self.assertEqual(data["Alpha PLA"]["note"], "alpha } note")
        self.assertEqual(data["Gamma PETG"]["note"], "gamma-note")

        status, body = call("/api/filaments/delete", {"name": "Generic Delta HF"})
        self.assertEqual(status, 200, body)

        status, body = call("/api/backup", {})
        self.assertEqual(status, 200, body)
        status, body = call("/api/backups")
        backups = json.loads(body)["backups"]
        self.assertTrue(any("config" in row["locations"] for row in backups))
        config_copy = next(row for row in backups if "config" in row["locations"])
        status, downloaded = call("/api/backups/" + config_copy["name"] + "?where=config")
        self.assertEqual(status, 200)
        downloaded_catalog = json.loads(downloaded)
        self.assertEqual(downloaded_catalog["Gamma PETG"]["note"], "gamma-note")
        self.assertIn("Alpha PLA", downloaded_catalog)

        status, body = call("/api/filaments/hide", {"name": "Gamma PETG"})
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["changed"])
        status, body = call("/api/filaments")
        hidden_list = json.loads(body)
        self.assertEqual(hidden_list["count"], 2)
        hidden = [row for row in hidden_list["filaments"] if row["hidden"]]
        self.assertEqual([row["name"] for row in hidden], ["Gamma PETG"])
        status, body = call("/api/filaments/unhide", {"name": "Gamma PETG"})
        self.assertEqual(status, 200, body)
        status, body = call("/api/filaments")
        restored = json.loads(body)
        self.assertEqual(restored["count"], 3)
        self.assertTrue(all(not row["hidden"] for row in restored["filaments"]))
        status, body = call("/api/filaments/hide", {"name": "Gamma PETG"})
        self.assertEqual(status, 200, body)
        status, body = call("/api/filaments/hidden/delete", {"name": "Gamma PETG"})
        self.assertEqual(status, 200, body)
        self.assertIn(b"unchanged", body)
        status, body = call("/api/filaments")
        after_delete = json.loads(body)
        self.assertEqual(after_delete["count"], 2)
        self.assertFalse(any(row["name"] == "Gamma PETG" for row in after_delete["filaments"]))
        status, body = call("/api/restore", {"name": config_copy["name"], "where": "config"})
        self.assertEqual(status, 200, body)
        status, body = call("/api/filaments")
        brought_back = json.loads(body)
        self.assertEqual(brought_back["count"], 3)
        self.assertTrue(any(row["name"] == "Gamma PETG" and not row["hidden"] for row in brought_back["filaments"]))

        status, body = call("/api/filaments/delete", {"name": "Alpha PLA"})
        self.assertEqual(status, 200, body)
        status, body = call("/api/filaments/delete", {"name": "Beta Nylon"})
        self.assertEqual(status, 200, body)
        status, body = call("/api/filaments/delete", {"name": "Gamma PETG"})
        self.assertEqual(status, 400)
        self.assertIn(b"cannot be emptied", body)

        status, body = call("/api/restore", {"name": config_copy["name"], "where": "config"})
        self.assertEqual(status, 200, body)
        _, data = parsed(catalog.discover([self.home]))
        self.assertIn("Alpha PLA", data)
        self.assertIn("Gamma PETG", data)
        self.assertNotIn("Generic Delta HF", data)

        status, body = call("/api/backups/delete", {"name": config_copy["name"]})
        self.assertEqual(status, 200, body)
        status, body = call("/api/backups")
        names = [row["name"] for row in json.loads(body)["backups"]]
        self.assertNotIn(config_copy["name"], names)
        status, body = call("/api/backups/delete", {"name": config_copy["name"]})
        self.assertEqual(status, 400)

        status, body = call("/api/restart", {})
        self.assertEqual(status, 409)
        if not Path("/bin/systemctl").exists():
            status, body = call("/api/reboot", {})
            self.assertEqual(status, 409)
            self.assertIn(b"/bin/systemctl", body)
        status, body = call("/api/filaments", {"name": "x"}, content_type="text/plain")
        self.assertEqual(status, 400)

        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        missing = server.build_status(catalog.discover([empty]), [empty])
        self.assertFalse(missing["found"])
        self.assertIn("does not use that catalog", missing["message"])


if __name__ == "__main__":
    unittest.main()
