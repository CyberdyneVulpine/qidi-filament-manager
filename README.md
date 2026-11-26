# Filament Manager

A small page for the QIDI touchscreen filament list. It runs on the printer. From another computer on the same network, open `http://<printer-address>:8091/`.

The load menu does not read Moonraker's config folder. On current Plus 5, Max 4, and other printers that use this touchscreen client, `qidiclient` opens:

```text
/home/qidi/QIDI_Client/resource/filament_config/filament-0.4.json
```

Older images use `/home/mks` with the same path under that home. The app uses whichever of those files it finds. If neither exists, the page says this printer's touchscreen does not use that catalog.

Python 3.9's standard library is enough. Nothing is installed with pip.

## What the page does

- Lists every material, with search and a vendor filter. Hide parks a material in `filament-manager/state/hidden.json` and takes it out of the load menu. Unhide puts that same entry back. Show hidden lists the parked ones. A slot that is using a material cannot be hidden, because Klipper shuts down when that id is missing from the catalog. Each row shows type, id, vendor, nozzle range, and chamber temperature. Buttons and links use Fluidd's primary color, `uiSettings.theme.color` in Moonraker's `fluidd` database. The page checks it about every five seconds. If that setting is missing, the color stays Fluidd's default blue.
- Adds a material by copying one that is already in the file. You set a single-word vendor, the material, and the stored type. The load menu shows the vendor and material as two parts. After you select the row, the filament line is both together. The id is optional: leave it blank, or use Generate, and the page takes the copied material's id and counts up its number to the next unused one (`QFA01` becomes `QFA02`). Nozzle and chamber are optional.
- Edits those same fields on an existing row.
- Removes a row after a confirmation that names the material. The last row cannot be removed. A row that a slot is using, according to `filament_slotN` in `printer_data/config/saved_variables.cfg`, is marked In use. It cannot be removed or given a different id, and a backup without its id cannot be restored. Loading a slot whose id is not in the catalog shuts Klipper down.
- Stages adds, edits, removals, hides, unhides, and restores. The printer's catalog file stays as it is until Apply Changes. Apply saves one backup of that file, writes the staged catalog, then reloads Klipper and the touchscreen. Back up now still saves a copy without editing. One copy sits beside the catalog. The other goes in that user's `printer_data/config`, which Fluidd can already show. The page lists each backup once, with where its copies are, and can download, restore, or delete it. Delete removes both copies. The installer saves one backup the first time it runs, marked Install on the page. That one cannot be deleted from the page. Re-running the installer makes a new one only if both of its copies are gone.
- Apply Changes stays in the bottom-right corner while you scroll. It sends `FIRMWARE_RESTART` through Moonraker, waits for Klipper to report `ready`, then restarts `qidi-client.service`. It takes about 20 seconds. Klipper keeps its own copy of the catalog, read only when it starts. Selecting a material that copy does not have shuts Klipper down. Deleting a hidden material does not change the load menu by itself. [touchscreen-klipper-materials.md](https://github.com/CaelanBorowiec/Qidi-Print-Configs/blob/main/docs/touchscreen-klipper-materials/touchscreen-klipper-materials.md) has the details.
- Shows a banner when the catalog has changed since Klipper or the touchscreen last read it. Klipper counts as up to date after its service starts or after an Apply from this page in the current boot. A `FIRMWARE_RESTART` sent from somewhere else is not seen, so the banner can stay up until the next Apply.
- The arrow on the edge of Apply Changes opens two more actions. Restart touchscreen runs `systemctl restart qidi-client.service` only. It is refused while Klipper has an older catalog, and is still useful for clearing a stuck popup. Reboot printer runs `systemctl reboot`. The page says the printer is rebooting, then the reboot runs about two seconds later.
- All three actions wait while Moonraker on `127.0.0.1:7125` reports a print as running or paused, and the page shows a single "printer is busy" message. `print_stats.state` must be `standby`, `complete`, `cancelled`, or `error`. Apply is also allowed whenever Klipper is shut down, so it can recover from the crash described above.

The first time the app reads the catalog, it records the filament ids already in the file under `filament-manager/state/` on that printer. Ids that show up later are marked Custom. That record stays on the printer.

## Install

From this directory, on a computer that can SSH to the printer:

```bash
bash install.sh qidi@printer
```

Use the account you normally use for SSH. It does not have to be the account that owns the catalog.

From Windows 10 or 11, use PowerShell instead:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 qidi@printer
```

It uses the `ssh` and `scp` that come with Windows. If they are missing, turn on OpenSSH Client under Settings > Apps > Optional features. The script copies the files with Unix line endings, then runs `install.sh` on the printer, so the steps and password prompts are the same. `-ExecutionPolicy Bypass` applies only to that one run.

If that password has never been changed, the factory login is:

- Plus 5 and Max 4: user `qidi`, password `qiditech`
- Plus 4 and the 3-series: user `mks`, password `makerbase`

`sudo` asks for that same password.

The installer asks for it several times. SSH opens a new connection for each step, and each connection asks again. After the files are copied, `sudo` on the printer asks once more. The script prints which step is about to ask. A repeated prompt is the next step. A wrong password is reported as incorrect. The installer does not write the password into the app.

On the printer itself:

```bash
bash install.sh
```

The installer finds the catalog, sees whether `qidi` or `mks` owns it, and copies the app to `/home/<owner>/filament-manager/`. The systemd service runs as that user. Re-running `install.sh` updates the files and restarts the service. A port you already set in the unit is kept.

## Port

The service listens on port **8091**. To use another port:

```bash
sudo systemctl edit filament-manager.service
```

```ini
[Service]
Environment=FILAMENT_MANAGER_PORT=8092
```

Then:

```bash
sudo systemctl restart filament-manager.service
```

Open `http://<printer-address>:<port>/`.

## Touchscreen restart

Restarting the screen needs root. When `qidi-client.service` is installed, `install.sh` adds this sudoers rule:

```text
/bin/systemctl restart qidi-client.service
```

The page will not restart the client while a print is active. If the unit is not on the printer, the app still runs and Restart touchscreen stays off in the Apply Changes menu.

Apply Changes needs no extra rule for Klipper. Moonraker's `/printer/firmware_restart` takes the request without a login on the local network. Before restarting Klipper, Apply checks that the touchscreen rule is in place, so it does not leave Klipper restarted with the screen still on the old list.

## Printer reboot

Rebooting needs root. `install.sh` also allows this one command:

```text
/bin/systemctl reboot
```

That rule is installed even when `qidi-client.service` is missing. A printer that already has the app needs `install.sh` again before Reboot printer is permitted. This is a Linux reboot. Apply Changes is enough for catalog edits.

## Firmware updates

A firmware package can replace `QIDI_Client` and can remove a unit under `/etc`. This app lives outside `QIDI_Client`. If an update removes the service, run `install.sh` again.

The catalog file itself is inside `QIDI_Client`, so an update can replace `filament-0.4.json`. Backups in `printer_data/config` are outside that tree. Restore one from the page, then Apply Changes.

## Uninstall

```bash
bash uninstall.sh qidi@printer
```

From Windows, `powershell -ExecutionPolicy Bypass -File uninstall.ps1 qidi@printer`. Or, on the printer, `bash uninstall.sh`. From another computer this asks for the password the same way: once to copy the script, again to connect, then once more for `sudo`. It stops the service, removes the unit, and removes the sudoers rule. It leaves `/home/<owner>/filament-manager` and the catalog file. Delete the app directory yourself if you want the files gone.

Service logs:

```bash
journalctl -u filament-manager.service -e
```

## Network

The service listens on every interface and does not ask for a login. On a stock QIDI, Fluidd and Moonraker are already reachable the same way. Do not forward this port to the internet.
