"""beatstudio session cleanup - run once daily via cron.

Nothing removes old session data today - a session's raw upload, decoded
vocal, project.json, and rendered WAVs (plus the published before/after
in RESULTS_DIR) all just accumulate forever. With 8-minute recordings now
allowed (~6x the old 30s cap's file size) and the box already tight on
disk, this bounds it: anything older than RETENTION_DAYS gets deleted,
both the session working directory and its published results together.

Age is read from status.json's updated_at (last real activity - a
session someone keeps re-rendering stays fresh); a missing or corrupt
status.json falls back to the directory's own mtime so nothing orphaned
survives indefinitely just because its status file broke.
"""
import json
import os
import shutil
import time

SESSIONS_DIR = "/root/beatstudio_sessions"
RESULTS_DIR = "/var/www/larzos/beatstudio/results"
RETENTION_DAYS = 7


def session_age_days(d):
    status_path = os.path.join(d, "status.json")
    try:
        with open(status_path) as f:
            updated_at = json.load(f)["updated_at"]
    except (OSError, ValueError, KeyError):
        updated_at = os.path.getmtime(d)
    return (time.time() - updated_at) / 86400.0


def main():
    if not os.path.isdir(SESSIONS_DIR):
        return
    removed = 0
    for session_id in os.listdir(SESSIONS_DIR):
        d = os.path.join(SESSIONS_DIR, session_id)
        if not os.path.isdir(d):
            continue
        if session_age_days(d) < RETENTION_DAYS:
            continue
        shutil.rmtree(d, ignore_errors=True)
        results_d = os.path.join(RESULTS_DIR, session_id)
        if os.path.isdir(results_d):
            shutil.rmtree(results_d, ignore_errors=True)
        removed += 1
    print("beatstudio cleanup: removed %d session(s) older than %d day(s)" % (removed, RETENTION_DAYS))


if __name__ == "__main__":
    main()
