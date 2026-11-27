# Third-party notices

LogLoss Watcher is licensed under the MIT licence (see `LICENSE`). It uses the following work by
others. All of it is permissive and allows use, change and redistribution with the notice kept.

## What is used

| Component | Used for | Licence | Source |
|---|---|---|---|
| pylogix | reading the PLC trigger of event-started data logs (read-only) | Apache-2.0 | https://github.com/dmroeder/pylogix |
| Python, incl. tkinter and ctypes | the program itself, the GUI, the tray icon | PSF License | https://www.python.org/ |
| Tcl/Tk | the toolkit behind tkinter | Tcl/Tk licence (BSD-style) | https://www.tcl-lang.org/ |
| PyInstaller bootloader | starts `LogLoss_Watcher.exe` | GPL-2.0-or-later with the bootloader exception | https://pyinstaller.org/ |

Run from source, pylogix is installed by you with `pip` and none of it is copied into this
repository; its licence comes with it.

## The .exe

`LogLoss_Watcher.exe` contains copies of Python, Tcl/Tk and pylogix, so passing the exe on means
passing their licences on too. `build_exe.bat` writes the full texts to
`dist\LogLoss_Watcher\THIRD_PARTY_LICENSES.txt` (made by `tools/collect_licenses.py` from the
packages that were actually bundled). **Keep that file in the folder when you copy it to an HMI
PC.** Apache-2.0 (pylogix) asks for this in section 4(a).

The PyInstaller bootloader exception lets the built exe be shared under any licence; it does not
make the exe GPL.

## Trademarks

Rockwell Automation, FactoryTalk, FactoryTalk View, Studio 5000 and Logix are trademarks of
Rockwell Automation, Inc. They are used here only to say which software this tool works with.
LogLoss Watcher is not affiliated with or endorsed by Rockwell Automation.
