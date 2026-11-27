# Changelog

## 2.0.0
- New look, the same as the other AnythingOT tools: navy header with the **Anything**OT
  wordmark, orange accent buttons, light grey window, flat tabs and tables, navy tooltips,
  footer with © AnythingOT and the version.
- Status colours are unchanged on purpose: badges, row colours and the red Critical banner
  still mean OK / warning / critical.
- Uses Tk's `clam` theme on every Windows version (was `vista` / `winnative`), so the window
  looks the same on Windows 7, 10 and 11.
- No change to monitoring, history, configuration or the PLC access (still read-only).
  The configuration file from 1.6.x is used as it is.

## 1.6.1
- Can be built as `LogLoss_Watcher.exe` (`build_exe.bat`, PyInstaller).
- As .exe, configuration and history are kept in the folder of the .exe
  (not in the temporary folder of a one-file build).
- "Start with Windows" starts the .exe directly when running as .exe.
- Application icon and Windows version information for the .exe.

## 1.6
- PC and datalog service restart tracking (history category "Service"):
  PC restarted, datalog service stopped / started, with PC restart vs. HMI server restart.
  Works without admin rights (restart seen as a new process id).
- Incident report (TXT): summary, timeline, data set files with last write time,
  PC / service restarts, coverage, watcher settings and status, window in local time and UTC.

## 1.5 and earlier
Internal versions, not tracked here.
