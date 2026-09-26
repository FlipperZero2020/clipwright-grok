"""clipwrightd — Grok Bot's Telegram front door for the Clipwright engine.

This package is the clipwright-grok fork: the same engine, run and kept
alive by Grok Bot. The live @username is whatever you registered; nothing
here invents one. Supervise a ``live-src`` checkout with
``python3 -m clipwrightd.ops``. Stdlib only (urllib, sqlite3, threading,
fcntl). Long polling, no webhook.
"""
