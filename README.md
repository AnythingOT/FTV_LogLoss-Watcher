# FTV_LogLoss-Watcher

**LogLoss Watcher – for FactoryTalk View SE Data Log models**

A small, read-only watchdog for the built-in Data Log models of FactoryTalk View SE
(file sets, not DataLog Pro – see FTV_DLP-Reporting-Tool for that).
It notices when a data log silently stops logging, raises a popup and keeps a history
that shows when, for how long and around what it happened.

- Watches the data set files of each data log (does the newest set keep growing?)
- Reads the trigger of event-started data logs from the PLC (pylogix, **read-only**)
- Judges only while the SE client is running (plus a start-up grace time)
- Popup, tray icon, PIN-locked setup, history with local time **and** UTC
- Coverage report: time logged vs. time it should have logged, per data log per day
- PC and datalog service restart tracking
- Incident report: one TXT around an incident, for support

It never writes to the PLC, never starts or stops data logs and never opens the `.DAT` files for reading.

<img width="1920" height="1020" alt="image" src="https://github.com/user-attachments/assets/c3c1fad0-40e0-4425-a565-2a039aed9eff" />

## Requirements

| | |
|---|---|
| OS | Windows 7 SP1, 10, 11 (the OSes FactoryTalk View SE 11 runs on) |
| Python | 3.8 or newer (3.8 is the last Python for Windows 7) – not needed when using the .exe |
| Package | `pylogix` (only for data logs with a PLC trigger) |
| Rights | No admin rights needed |
| Load | ~40–70 MB RAM, well under 1 % CPU, below-normal priority |

## Quick start (Python)

```
pip install --user pylogix
python logloss_watcher.py --simulate     # try it without PLCs
python logloss_watcher.py                # real mode
```

Default PIN is `1234` – change it under **Settings > Security**.

## Build the .exe

On a Windows build PC with internet and **Python 3.8.10 (64-bit)**, run `build_exe.bat`.
It creates a clean virtual environment, installs PyInstaller 6.9.0 and pylogix, and builds
`dist\LogLoss_Watcher\`.

Copy the **whole** `dist\LogLoss_Watcher\` folder to the HMI PC, into a folder the user can
write to (for example `C:\LogLoss_Watcher\`, not `C:\Program Files`). Configuration and
history are kept next to the .exe.

If Windows 7 reports a missing `api-ms-win-crt-*.dll`, install the Universal C Runtime update.
If an exe built on Windows 10 does not start on Windows 7, build it on Windows 7.

**Release build.** Push a tag like `v1.6.1` (or run *Build Windows exe* in the Actions tab):
`.github/workflows/release.yml` runs `build_exe.bat` with Python 3.8 and attaches
`LogLoss_Watcher-v1.6.1.zip` (the whole folder) with a checksum to the release. It first checks
that the tag matches `VERSION` in `logloss_watcher.py`, both versions in `version_info.txt` and a
`## 1.6.1` section in `CHANGELOG.md`, so bump all four together. GitHub builds on Windows
Server: test the zip on a Windows 7 HMI PC before relying on it there.

## Setup order

1. **Settings** – datalog folder, retention days, process names, *Log-only mode* for the first week.
2. **PLCs** – topic (FactoryTalk shortcut name), IP, slot, connection size
   (Studio 5000 Logix Emulate: 504, and enable *Listen on EtherNet/IP encapsulation ports* in FactoryTalk Linx).
3. **Data Logs** – add each model, type *Macro* (always on) or *EXPR* (event-started: paste its ON expression).
4. After a week without false alarms, turn *Log-only mode* off.
5. **Settings > Start with Windows** – starts hidden in the tray.

Full operating notes: [README.txt](README.txt) (also shipped next to the .exe).

## Files

| File | |
|---|---|
| `logloss_watcher.py` | GUI (Tkinter) |
| `llw_core.py` | monitoring engine, expression parser, reports |
| `llw_tray.py` | tray icon (ctypes, no extra packages) |
| `build_exe.bat`, `version_info.txt`, `llw.ico` | .exe build |
| `.github/workflows/release.yml` | release build on GitHub (runs `build_exe.bat`) |
| `tools/collect_licenses.py` | writes `THIRD_PARTY_LICENSES.txt` during the .exe build |
| `THIRD_PARTY_NOTICES.md` | software by others and its licences |

Created at runtime and **not** part of the repository: `logloss_watcher_config.json`
and `history\`. They contain data log names, PLC addresses, trigger expressions and
production history – keep them out of version control (see `.gitignore`).

## Known anomalies – FactoryTalk View SE Data Log models (file sets)

Check the release notes of your FactoryTalk View SE version for details.
LogLoss Watcher does not fix these; it detects when logging stops.

- Logging stops while the oldest data is purged at midnight (fixed in 13.00)
- A file set can become unusable; logging resumes only after the newest
  file set is moved out of the data log folder, with the HMI server stopped
  first. Move it, don't delete it (seen in 11.00)
- "Primary directory unavailable or unwritable" error on DataLogOn
  (first reported 9.00, still listed as known in 11.00)
- Custom data log folder without LOCAL SERVICE access slows the secondary
  HMI server start (first reported 9.00, fixed in 12.00: now shows access denied)
- Accented characters in string values show as "?" in File Viewer and CSV export
  (fixed in 15.00; display only, data is logged)
- Daylight saving change: a trend misses one hour (first reported 9.00;
  the data is still in the log)
- ODBC only: logging does not resume after the database reconnects
  (first reported 8.20)

## Disclaimer

Not affiliated with or endorsed by Rockwell Automation. FactoryTalk is a trademark of
Rockwell Automation, Inc. This tool only observes; it is not a substitute for a supported
FactoryTalk View version or for Rockwell support.

## License

MIT – see [LICENSE](LICENSE).

Uses pylogix (Apache-2.0), Python and tkinter (PSF License) and Tcl/Tk (BSD-style):
see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The .exe build writes their full texts to
`THIRD_PARTY_LICENSES.txt` next to the .exe; keep it there when you copy the folder.
