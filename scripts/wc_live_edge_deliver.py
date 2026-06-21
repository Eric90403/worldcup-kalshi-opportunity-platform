#!/usr/bin/env python3
"""
wc_live_edge_deliver.py — Tails the live-edge commentary log and prints new lines.

Designed to run as a no_agent=True Hermes cron job (every 2 min). Non-empty stdout
is delivered to Telegram verbatim. Empty stdout = silent (nothing to report).

Reads data/live_edge_commentary.log from the offset stored in
/tmp/wc_live_edge_deliver_offset, prints new lines, updates the offset.
"""
import os, sys

LOG = './data/live_edge_commentary.log'
OFFSET_FILE = '/tmp/wc_live_edge_deliver_offset'

def main():
    # Read last-delivered offset
    try:
        with open(OFFSET_FILE) as f:
            offset = int(f.read().strip() or '0')
    except (FileNotFoundError, ValueError):
        offset = 0

    # If the log file shrank or was recreated, reset offset
    try:
        size = os.path.getsize(LOG)
    except FileNotFoundError:
        return  # no log yet, silent
    if size < offset:
        offset = 0

    # Read new content
    try:
        with open(LOG) as f:
            f.seek(offset)
            new_content = f.read()
            new_offset = f.tell()
    except Exception:
        return  # silent on error

    if new_content.strip():
        # Print without trailing newline (cron adds one)
        sys.stdout.write(new_content.rstrip() + '\n')
        sys.stdout.flush()
        with open(OFFSET_FILE, 'w') as f:
            f.write(str(new_offset))
    # else: silent — nothing new to deliver

if __name__ == '__main__':
    main()
