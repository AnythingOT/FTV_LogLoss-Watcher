# Changelog

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
