"""Dummy long-running process used by the dashboard launch tests.

Lives long enough to observe a running state, then exits on its own.
"""

import time

for i in range(1000):
    print(f"dummy heartbeat {i}", flush=True)
    time.sleep(1)

print("dummy exiting", flush=True)
