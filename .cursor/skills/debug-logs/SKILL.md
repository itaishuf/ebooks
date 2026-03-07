---
name: debug-logs
description: Read and interpret ebookarr production logs to debug stuck jobs, pipeline failures, and service issues. Use when investigating a bug using logs, a user reports a job not completing, or when asked to check what happened to a specific request.
---

# Debug Production Logs

## Access logs

```bash
docker compose exec ebookarr sh -lc 'tail -n 200 /data/books.log'
```

To follow live:
```bash
docker compose logs -f ebookarr
```

## Log format

```
TIMESTAMP, [file.py:LINE - function()], LEVEL, job=<uuid|"-">, user=<uuid|"-">, "message"
```

`job=-` means the log line is not associated with any download job (e.g. startup, search).

## Tracing a specific job

Every line for a job carries the same `job=<uuid>`. Grep by it:

```bash
docker compose exec ebookarr sh -lc 'grep "job=<uuid>" /data/books.log'
```

Note: `service.py` logs `"Created Goodreads download job for <user_id>"` — the value after "for" is the **user ID**, not the job ID. The job UUID appears on the very next line as `job=<actual-uuid>`.

## Normal pipeline log sequence

A healthy job produces these entries in order:

1. `get_isbn` called + returned
2. `get_book_md5` called + returned (epub first, pdf fallback)
3. `choose_libgen_mirror` called + returned
4. `get_libgen_link` called + returned
5. `download_book_using_selenium` called → "Starting Selenium download..." → "LibGen Selenium page attempt 1/2..." → "Detected download candidate..." → "Detected completed download..." → "Selenium download completed..." → **`sync_wrapper() return value`** (this line confirms the function fully returned)
6. `send_to_kindle` called + returned
7. `send_to_kindle return value: None` (success)

## Diagnosing a hung job

**Symptom**: Job stuck on "Downloading", no error in log, user sees infinite spinner.

**Check**: Is there a `sync_wrapper() return value` line for `download_book_using_selenium` after "Selenium download completed"?

- **If yes** → hang is downstream (SMTP / `send_to_kindle`)
- **If no** → the function's `finally` block is blocking before it returns. This was previously caused by `driver.quit()` hanging on the WebDriver HTTP teardown. If it recurs, check `_force_quit_driver()` in `download_with_libgen.py`.

## Diagnosing a failed job

If `status=error` in the UI, look for `ERROR` or `WARNING` level lines for that job UUID. Common patterns:

| Log message | Meaning |
|---|---|
| `No libgen download found matching ISBN` | All md5 links were dead; retry later |
| `Failed to find book in libgen` | `NoSuchElementException` — libgen page layout changed |
| `EmailDeliveryError` | Gmail SMTP failed; check Gmail app password |
| `file size: 0.0KB` | File was deleted before `send_to_kindle` read it (race condition or cleanup ran early) |
| `ManualDownloadRequiredError` | Selenium never detected a downloaded file after all attempts |
| `No epub results for ISBN ... falling back to pdf` | Normal; epub unavailable, pdf used instead |

## Startup sequence (always present at boot)

```
bitwarden.py: Fetching secrets from Bitwarden vault
...
runtime_bootstrap.py: Anna's Archive mirror selected: <url>
```

If `Anna's Archive mirror selected` is missing, startup failed — the container likely exited. Check `docker compose ps`.

## Benign / ignorable warnings

- `selenium_manager.py: Error sending stats to Plausible` — LibGen's analytics call fails in the container's network. Always safe to ignore.
- `gather_page_status return value: [None, None, ...]` with some non-None — normal; some mirrors are down, the first live one is used.

## Mirror health at a glance

`gather_page_status` is called twice per job:
1. To pick a libgen mirror (8 URLs checked)
2. To find a live md5 download link (27 URLs checked)

A result like `[None, None, '[redacted-url]', ...]` means only one mirror responded. If **all** are `None`, the job will fail with `ConnectionError: No active libgen mirror found`.
