"""Generate a spammy, realistic-looking log file for e2e testing."""

import random
import datetime

START = datetime.datetime(2024, 3, 12, 8, 0, 0)

SERVICES = ["auth-service", "api-gateway", "db-pool", "cache", "worker"]
REQUEST_IDS = [f"req-{i:04d}" for i in range(1, 20)]
USER_IDS = [f"user_{i}" for i in [42, 99, 1337, 7, 500]]

def ts(offset_seconds):
    t = START + datetime.timedelta(seconds=offset_seconds)
    return t.strftime("%Y-%m-%d %H:%M:%S")


lines = []
t = 0

# --- Normal startup spam ---
for i in range(30):
    svc = random.choice(SERVICES)
    lines.append(f"{ts(t)} INFO  [{svc}] Heartbeat OK (uptime={i*10}s, mem=128MB)")
    t += 1

# --- Some debug noise ---
for i in range(20):
    lines.append(f"{ts(t)} DEBUG [db-pool] Connection pool check: 10/10 available")
    t += 1
for i in range(20):
    lines.append(f"{ts(t)} DEBUG [cache] Cache hit ratio: 0.{random.randint(80,99)}")
    t += 1

# --- First real error: DB connection starts failing ---
lines.append(f"{ts(t)} ERROR [db-pool] Failed to acquire connection (timeout=5000ms)")
t += 1
lines.append(f"{ts(t)} ERROR [db-pool] Failed to acquire connection (timeout=5000ms)")
t += 1
lines.append(f"{ts(t)} ERROR [db-pool] Failed to acquire connection (timeout=5000ms)")
t += 1

# --- Stack trace ---
lines.append(f"{ts(t)} ERROR [api-gateway] Unhandled exception in request handler")
t += 1
lines.append("Traceback (most recent call last):")
lines.append('  File "api/handler.py", line 87, in handle_request')
lines.append("    result = db.query(sql, params)")
lines.append('  File "db/pool.py", line 42, in query')
lines.append("    conn = self._pool.acquire(timeout=self.timeout)")
lines.append('  File "db/pool.py", line 118, in acquire')
lines.append("    raise TimeoutError(f\"No connections available after {timeout}ms\")")
lines.append("TimeoutError: No connections available after 5000ms")
t += 1

# --- Cascade: auth failures ---
for req_id in REQUEST_IDS[:8]:
    lines.append(f"{ts(t)} WARN  [auth-service] JWT verification failed for {req_id}: token expired")
    t += 1

# --- More spam during the outage ---
for i in range(15):
    lines.append(f"{ts(t)} ERROR [db-pool] Failed to acquire connection (timeout=5000ms)")
    t += 1
for i in range(10):
    lines.append(f"{ts(t)} INFO  [api-gateway] Returning 503 to client (service unavailable)")
    t += 1

# --- Second stack trace: retry exhausted ---
lines.append(f"{ts(t)} FATAL [worker] Retry limit exceeded, job failed permanently")
t += 1
lines.append("Traceback (most recent call last):")
lines.append('  File "worker/runner.py", line 33, in run_job')
lines.append("    self._execute_with_retry(job)")
lines.append('  File "worker/runner.py", line 71, in _execute_with_retry')
lines.append("    raise RuntimeError(f\"Job {job.id} failed after {MAX_RETRIES} retries\")")
lines.append("RuntimeError: Job job-8821 failed after 3 retries")
t += 1

# --- Blank lines and separator noise ---
lines.append("")
lines.append("---")
lines.append("")

# --- Recovery ---
lines.append(f"{ts(t)} INFO  [db-pool] Connection pool recovered (10/10 available)")
t += 2
lines.append(f"{ts(t)} INFO  [api-gateway] Health check passed")
t += 2
for i in range(20):
    lines.append(f"{ts(t)} INFO  [{random.choice(SERVICES)}] Heartbeat OK (uptime={300+i*10}s, mem=130MB)")
    t += 1

print("\n".join(lines))
