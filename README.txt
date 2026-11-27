LOGLOSS WATCHER 1.6.1 - for FTView SE DataLog Models
====================================================

FILES (keep together in one folder on C:, e.g. C:\Users\<you>\LogLossWatcher\ -
       NOT on the drive that holds the datalogs)
  logloss_watcher.py   the program
  llw_core.py          engine
  llw_tray.py          tray icon
  Created automatically:
  logloss_watcher_config.json         configuration
  history\history_YYYY-MM-DD.txt      history, one file per day (kept 365 days)
  history\coverage_YYYY-MM-DD.json    coverage figures per day
  history\heartbeat.txt               used to detect when the watcher was not running

UPGRADE FROM DL WATCHDOG
  Put the files in the old folder and start logloss_watcher.py. The old configuration
  (dl_watchdog_config.json) is taken over automatically. The old "start with Windows"
  entry is replaced when you tick Start with Windows again.

INSTALL (no admin needed)
  Python 3.8 or newer. Windows 7: Python 3.8 is the last version that runs.
  pylogix:   pip install --user pylogix
  No internet on the PC? On another PC:  pip download pylogix
  copy the .whl over and run:           pip install --user pylogix-<version>.whl

START
  python logloss_watcher.py --simulate   try without PLCs (PLCs tab: double-click a tag)
  python logloss_watcher.py              real mode
  Monitoring is always ON when the watcher starts. GATE: nothing is judged while the SE client
  (runtime) is closed; checks begin 60 s after the runtime is seen running (Settings).
  Closing or minimizing the window hides it in the tray (coloured dot):
     green = all OK   red = critical   orange = warning / PLC unknown   grey = monitoring stopped
  Double-click the tray icon to open, right-click for the menu.
  Exit and Stop monitoring need the PIN while setup is locked.
  Default PIN 1234 -> change it (Settings > Security).

SETUP ORDER
  1. Settings : datalog folder, "FTView deletes data sets older than (days)",
                process names (Check now), Log-only mode for the first week.
                "Check folders" (right side) shows what the watcher finds in every data log folder.
  2. PLCs     : Topic = FactoryTalk shortcut name, IP, slot, Connection size.
                Connection size: 0 = automatic. Studio 5000 Logix Emulate: 504, slot of the
                emulated controller, and in FactoryTalk Linx enable
                "Listen on EtherNet/IP encapsulation ports" (pylogix issue #221).
                Test, then Browse PLC... / Add tag..., Read now.
  3. Data Logs: Add (new data logs start DISABLED), choose folder and type:
       Macro - started by the startup macro, always on (paused while the SE client is closed)
       EXPR  - started/stopped by an event: paste its ON expression, Validate, Evaluate now.
               HMI tags (no [TOPIC]) cannot be read and are treated as EMPTY:
               {HMI tag} == "text" is FALSE, the tag on its own is FALSE.
     Defaults for new data logs: not started 2 min, not grown 3 min (fits 1 s logging).
     Set "NOT GROWN" to at least 3 x the logging interval of the FTView model.
  4. After one week without false issues: untick Log-only mode.
  5. Settings > Start with Windows (starts hidden in the tray).

STATUS COLUMNS
  Only ENABLED data logs are shown.
  State       Started / Stopped / Critical / Warning / Unknown (PLC not reachable) / Paused (runtime
              not running or starting)
  NOTE: start/stop is INFERRED from file growth. Exact datalog start/stop times are in the DAT files.
  Click a column header to sort (click again to reverse).
  Type        Macro / EXPR
  Trigger     Triggered / Waiting / Always (Macro) / ? (unknown)
  Size        current size of today's data set
  Last growth time since the data set last grew
  Current file / File time   the .DAT file written last and its timestamp

POPUP
  Drag it anywhere with the mouse; it returns to the Settings position the next time it opens.
  "Check now" (Status tab, right) runs a check immediately.

EVENT-TRIGGERED DATA LOGS (EXPR)
  FactoryTalk events fire only when the trigger goes 0 -> 1 (like a one-shot). If an EXPR data log
  is stopped while its trigger is 1 (manually, or by a service hang), the Event Detector does not
  restart it until the trigger goes 0 -> 1 again. The Critical message says so. Short-term fix:
  EventOff / EventOn of the events file (e.g. in the startup macro).

HISTORY AND EXPORT
  History tab shows yesterday + today; enter other days and press Load.
  Filter: All / OK / Critical / Warning / Critical + Warning.
  Every entry has local time AND UTC (FactoryTalk Diagnostics exports are in UTC).
  Logged: start/stop, issues, PLC not reachable / back, every snooze (Windows user and
  active issues), "engine hang suspected" (several data logs stopping within 60 s),
  data set deletion check, issues near data set rollover/deletion, watcher downtime.
  File > Export history / coverage: period, severity, category, data logs, search, TXT or CSV.
  Coverage report: per data log per day - should log / logged / % / issues / snoozes.

PC AND DATALOG SERVICE RESTARTS (category "Service")  - new in 1.6
  Logged: "PC restarted at ...", "Datalog service ... stopped", "Datalog service ... started ...".
  A service start says whether the PC was restarted or only the HMI server / service.
  Without admin rights Windows usually hides a service's start time; the restart is then seen
  as a new process id and the time is given as "between <last check> and <this check>".
  Restarts while the watcher was not running are reported when it starts again.

INCIDENT REPORT  - new in 1.6
  History tab > "Incident report..." (or File menu). Select a Critical entry first to use its time.
  One TXT with: summary, full timeline, the data set files of each data log with their last write
  time (shows when logging really stopped), PC / service restarts, coverage, watcher settings and
  the status at report time. The window is printed in local time AND UTC - use the UTC times to
  export the matching FactoryTalk Diagnostics messages. Trigger expressions are left out unless
  you tick "Include trigger expressions" (they contain PLC tag names).

RESOURCES
  ~40-70 MB RAM, well under 1 % CPU, runs at below-normal priority. Reads only the folder
  listing and the 2-3 files of today's data set per check. Never opens the .DAT files.
  PLC: one small read per controller per check; an unreachable PLC is retried every 60 s.
  PLC access is READ-ONLY.

CHANGES IN 1.6
  + PC / datalog service restart tracking (history category "Service")
  + Incident report (TXT) for support cases

CHANGES IN 1.6.1
  + Can be built as LogLoss_Watcher.exe (build_exe.bat). As .exe, config and history are kept
    in the folder of the .exe; Windows start-up starts the .exe directly.
