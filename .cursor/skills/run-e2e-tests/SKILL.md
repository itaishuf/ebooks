---
name: run-e2e-tests
description: Run and fix E2E tests in a loop for the ebookarr project. Use when the user asks to run E2E tests, fix failing tests, or says "run tests until they pass". Knows common failure modes: Anna's Archive DNS, Selenium Firefox download dir, Gmail app password, ISBN extraction.
---

# Run E2E Tests

## Command

```bash
uv run pytest -m e2e -v 2>&1 | tee /tmp/test-output.txt
```

E2E tests are slow (10–25 minutes each run due to Selenium). Read test output from `/tmp/test-output.txt` while they run.

## Fix loop

When asked to run and fix until passing:
1. Run tests → read output → identify failures → fix code → run again
2. Repeat until all tests pass
3. Do NOT stop between iterations to ask for confirmation

## Common failure modes and fixes

### 1. Anna's Archive domain not resolving (DNS blocked)
**Symptom**: `aiohttp.ClientConnectorError` or DNS lookup failure for `annas-archive.*`  
**Fix**: The server auto-selects a healthy mirror at startup from `settings.annas_archive_mirrors`. If all mirrors are down, reorder or add working mirrors in `config.py` and restart.

### 2. Selenium downloading to wrong directory
**Symptom**: Test waits forever for file in `settings.download_dir`, file appears in `~/Downloads` or `~`  
**Fix**: Ensure Firefox options include:
```python
options.set_preference("browser.download.folderList", 2)
options.set_preference("browser.download.dir", settings.download_dir)
options.set_preference("browser.helperApps.neverAsk.saveToDisk",
                       "application/epub+zip,application/pdf,application/octet-stream")
```

### 3. `driver.close()` causing 120-second timeout
**Symptom**: Test hangs exactly 120 seconds after the download finishes  
**Fix**: Replace `driver.close()` with `driver.quit()`

### 4. Gmail SMTP `535 Username and Password not accepted`
**Symptom**: `EmailDeliveryError` / SMTP auth failure  
**Fix**: The Gmail app password has expired. The user must generate a new one at myaccount.google.com → Security → App passwords. Then update `GMAIL_PASSWORD` in `.env`.

### 5. ISBN extraction picking up wrong number
**Symptom**: `get_isbn` returns a number like `1738790966` (looks like a Unix timestamp)  
**Fix**: Ensure `get_isbn` uses JSON-LD structured data **first**:
```python
for script in soup.find_all('script', type='application/ld+json'):
    data = json.loads(script.string)
    isbn = data.get('isbn')
    if isbn:
        return isbn
```
Regex fallback on raw HTML can match image URL timestamps.

### 6. Download directory doesn't exist
**Symptom**: `FileNotFoundError` when Selenium tries to download  
**Fix**: Add `os.makedirs(settings.download_dir, exist_ok=True)` before creating the driver

## Test configuration

E2E tests require these `.env` values:
- `TEST_GOODREADS_URL` — a real Goodreads book URL
- `TEST_KINDLE_EMAIL` — real Kindle email to send to
- `GMAIL_PASSWORD` — valid Gmail app password

## Reading `books.log` for failures

The app logs via `@log_call` decorator. Each function logs its arguments on entry and its result on exit. To trace a failed request:
```bash
tail -100 books.log | grep -A5 "get_isbn\|get_book_md5\|download"
```
