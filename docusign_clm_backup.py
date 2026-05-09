"""
Docusign CLM (SpringCM) Bulk Backup Script
==========================================

Recursively downloads every document from a folder tree in Docusign CLM
(formerly SpringCM) to your local disk, preserving the folder structure.

WHY:
  Docusign CLM has no built-in bulk-export feature.  The web UI lets you
  download one document at a time, or zip a few at once — but anything more
  than a small folder is impractical, and zipping breaks if any subfolders
  are present.  This script automates the per-document download flow.

HOW IT WORKS:
  Uses Playwright to drive a real Chromium browser.  You log in manually
  (so SSO / Okta / MFA all work normally), point the script at your root
  folder, and it walks the tree, downloading each document via SpringCM's
  internal DownloadRedirect endpoint.  Falls back to the UI click flow
  when the direct URL won't work (rare).

KEY FEATURES:
  - Resume support: progress is checkpointed to disk after every document
    and folder.  If the script crashes, your session times out, or you hit
    Ctrl-C, just re-run it and it picks up where it left off.
  - Session-expiry detection: if Docusign kicks you out, the script pauses,
    prompts you to log back in, then continues from the same folder.
  - Per-document downloads (never bulk zips, which break on subfolders).
  - Doc-id (GUID) based dedup, not filename-based — so two docs with long
    names that truncate to the same filename still both get saved.

CONFIGURATION:
  Set the three required values in the CONFIG block below, OR via environment
  variables (CLM_HOST, CLM_AID, CLM_ROOT_FOLDER_ID).  See the README for how
  to find these values for your tenant.

USAGE:
  pip install -r requirements.txt
  python -m playwright install chromium
  python docusign_clm_backup.py
"""

import asyncio
import math
import os
import re
import time
from pathlib import Path
from playwright.async_api import async_playwright, Download, Page

# ═════════════════════════════════════════════════════════════════════════════
# CONFIG — set these for your tenant
# ═════════════════════════════════════════════════════════════════════════════
#
# You can either edit the values below or set them via environment variables:
#   CLM_HOST              e.g. "na11.springcm.com" or "eu11.springcm.com"
#   CLM_AID               your numeric account ID (see README to find it)
#   CLM_ROOT_FOLDER_ID    GUID of the folder you want to back up (see README)
#   CLM_ROOT_FOLDER_NAME  display name for that folder (used for the local
#                         output subdirectory; optional, defaults to "Root")
#   CLM_DOWNLOAD_DIR      where to save files (defaults to ./clm_backup)

# Docusign CLM/SpringCM hostname.  Common values:
#   na11.springcm.com  (US, most common)
#   na21.springcm.com  (US)
#   eu11.springcm.com  (EU)
# Find yours by logging into Docusign CLM and looking at the URL.
CLM_HOST = os.environ.get("CLM_HOST", "na11.springcm.com")

# Your account ID (numeric).  Visible in the URL as "?aid=XXXXX" after you
# navigate to any folder.  See the README for screenshots.
AID = os.environ.get("CLM_AID", "")

# GUID of the root folder you want to back up.  Navigate to that folder in
# Docusign CLM, open the browser console, and run:
#   window.location.search.match(/folderId=([a-f0-9-]+)/)?.[1]
# Or see the README for an alternative way using the page source.
ROOT_FOLDER_ID = os.environ.get("CLM_ROOT_FOLDER_ID", "")

# Display name for the root folder.  Used as the top-level local directory
# name and in console output.  Cosmetic only.
ROOT_FOLDER_NAME = os.environ.get("CLM_ROOT_FOLDER_NAME", "Root")

# Where to save downloaded files.  A subdirectory named after ROOT_FOLDER_NAME
# is created inside this directory.
DOWNLOAD_DIR = Path(os.environ.get("CLM_DOWNLOAD_DIR", "clm_backup")).resolve()

# How long to wait (in seconds) for you to finish logging in on first launch.
LOGIN_WAIT_SEC = 120

# Maximum subfolder nesting depth.  Bump this if your tree is deeper than 10.
MAX_DEPTH = 10

# Master slowness multiplier.  Increase if your network or the CLM site is
# slow/flaky.  1.0 = baseline, 2.0 = twice as patient everywhere.
SLOW_MODE = 1.0

FOLDER_NAV_TIMEOUT_SEC = int(30 * SLOW_MODE)
PAGE_NAV_TIMEOUT_SEC   = int(60 * SLOW_MODE)
DOWNLOAD_TIMEOUT_SEC   = int(45 * SLOW_MODE)
POLL_INTERVAL_SEC      = 0.5

# Derived URLs (don't edit unless you know what you're doing)
CLM_BASE_URL     = f"https://{CLM_HOST}"
SPRINGCM_URL     = f"{CLM_BASE_URL}/atlas/d/Documents/BrowseDocuments?aid={AID}"
DOWNLOAD_API_URL = f"{CLM_BASE_URL}/atlas/action/DownloadRedirect"

# ═════════════════════════════════════════════════════════════════════════════
# Runtime state
# ═════════════════════════════════════════════════════════════════════════════

downloaded          = 0
skipped             = 0
failed              = 0
visited             = set()
failed_docs         = []   # list of (folder_path, filename)
needs_manual_review = []   # list of (folder_path, filename) — no Download in menu


def sanitise(name: str) -> str:
    """
    Make a name safe for use as a filename / directory name.  Replaces
    forbidden characters with underscore, strips whitespace, truncates to
    80 chars, then strips again so the result is idempotent.
    """
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return name[:80].strip()


def _norm_for_compare(s: str) -> str:
    """Normalise a folder name for comparison only.  Lowercase + collapse whitespace."""
    if not s:
        return ""
    return re.sub(r'\s+', ' ', s.lower()).strip()


def _names_match(a: str, b: str) -> bool:
    """
    Loose comparison: True if the two names refer to the same folder.
    Accepts an exact match OR either side being a prefix of the other,
    so a value truncated by sanitise() still matches its full version.
    """
    na = _norm_for_compare(a)
    nb = _norm_for_compare(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 8 and nb.startswith(na):
        return True
    if len(nb) >= 8 and na.startswith(nb):
        return True
    return False


# ═════════════════════════════════════════════════════════════════════════════
# Checkpoint / resume support
# ═════════════════════════════════════════════════════════════════════════════

CHECKPOINT_DOCS_FILE    = DOWNLOAD_DIR / ".completed_docs.txt"
CHECKPOINT_FOLDERS_FILE = DOWNLOAD_DIR / ".completed_folders.txt"

completed_docs    = set()   # doc_ids we've successfully downloaded
completed_folders = set()   # folder_ids we've fully processed (all docs + subfolders)


def load_checkpoints():
    """Load completed_docs and completed_folders from disk on startup."""
    global completed_docs, completed_folders
    if CHECKPOINT_DOCS_FILE.exists():
        completed_docs = {
            line.strip()
            for line in CHECKPOINT_DOCS_FILE.read_text().splitlines()
            if line.strip()
        }
    if CHECKPOINT_FOLDERS_FILE.exists():
        completed_folders = {
            line.strip()
            for line in CHECKPOINT_FOLDERS_FILE.read_text().splitlines()
            if line.strip()
        }
    if completed_docs or completed_folders:
        print(f"📂 Resuming: {len(completed_docs)} docs, "
              f"{len(completed_folders)} folders already complete")


def mark_doc_done(doc_id: str):
    """Record a successfully downloaded doc_id (in memory + appended to disk)."""
    if not doc_id or doc_id in completed_docs:
        return
    completed_docs.add(doc_id)
    try:
        CHECKPOINT_DOCS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CHECKPOINT_DOCS_FILE, "a") as f:
            f.write(doc_id + "\n")
    except Exception as e:
        print(f"      ⚠️  Could not write doc checkpoint: {e}")


def mark_folder_done(folder_id: str):
    """Record a fully processed folder_id (in memory + appended to disk)."""
    if not folder_id or folder_id in completed_folders:
        return
    completed_folders.add(folder_id)
    try:
        CHECKPOINT_FOLDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CHECKPOINT_FOLDERS_FILE, "a") as f:
            f.write(folder_id + "\n")
    except Exception as e:
        print(f"      ⚠️  Could not write folder checkpoint: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# Session expiry detection + re-auth pause
# ═════════════════════════════════════════════════════════════════════════════

class SessionExpiredError(Exception):
    """Raised when we detect Docusign CLM has kicked us out (session timeout)."""
    pass


async def is_session_valid(page) -> bool:
    """
    Return True if the page is still on a Docusign CLM document-browser URL
    with a non-error title.  Returns False if we've been redirected to Okta,
    to a login page, or to an error page like "Bad Request".
    """
    try:
        info = await page.evaluate("""
            () => ({ url: location.href || '', title: document.title || '' })
        """)
        url   = (info.get("url") or "").lower()
        title = (info.get("title") or "").lower()
        if "springcm.com" not in url and "docusign" not in url:
            return False
        if any(bad in title for bad in ("bad request", "unauthorized",
                                         "sign in", "error", "not found")):
            return False
        return True
    except Exception:
        return False


async def wait_for_reauth(page) -> bool:
    """
    Block until the user has logged back in and navigated back to the root
    folder.  Returns True on success, False if the user gives up.
    """
    print("\n" + "=" * 60)
    print("⚠️  SESSION EXPIRED — Docusign CLM kicked us out")
    print("=" * 60)
    print("Your work so far is saved.  To continue:")
    print("  1. In the browser window, log back into Docusign CLM")
    print(f"  2. Navigate to your root folder ({ROOT_FOLDER_NAME})")
    print("  3. Come back here and press Enter")
    print()
    print("Or press Ctrl-C to stop — your progress is saved and you can")
    print("restart the script later to resume from where you left off.")
    print()
    await asyncio.get_event_loop().run_in_executor(
        None, input, f"Press Enter once you're back on the {ROOT_FOLDER_NAME} page... "
    )
    await asyncio.sleep(2)
    for attempt in range(10):
        if await is_session_valid(page):
            total = await wait_for_stable_total_items(page, 15)
            if total >= 0:
                print(f"✅ Session restored — resuming ({ROOT_FOLDER_NAME} has {total} items)\n")
                return True
        print("⏳ Session not yet valid, waiting...")
        await asyncio.sleep(2)
    print("❌ Session still invalid after 20s.  Stopping — please restart the script.")
    return False


async def check_session_or_raise(page):
    """Check session validity; raise SessionExpiredError if expired."""
    if not await is_session_valid(page):
        raise SessionExpiredError()


# ═════════════════════════════════════════════════════════════════════════════
# Page state readers — these don't wait, they just read whatever is on screen
# ═════════════════════════════════════════════════════════════════════════════

async def read_folder_header(page) -> str:
    """
    Return the current folder's display name.  Uses document.title as the
    primary signal — it updates reliably with every folder navigation.
    Docusign CLM titles look like '{folder name} - Documents - Docusign CLM'
    so we strip everything from the first ' - ' onward.

    Falls back to the main content h1 if the title is unavailable or generic.
    """
    try:
        return await page.evaluate("""
            () => {
                const rawTitle = (document.title || '').trim();
                if (rawTitle) {
                    const suffixes = [
                        ' - Documents - Docusign CLM',
                        ' - Documents - DocuSign CLM',
                        ' - Documents - Docusign',
                        ' - Documents - DocuSign',
                        ' - Documents',
                    ];
                    for (const suf of suffixes) {
                        if (rawTitle.endsWith(suf)) {
                            const folderPart = rawTitle.slice(0, -suf.length).trim();
                            if (folderPart && folderPart !== 'Documents' && folderPart !== 'Docusign CLM') {
                                return folderPart;
                            }
                        }
                    }
                    if (rawTitle !== 'Documents' && rawTitle !== 'Docusign CLM') {
                        return rawTitle;
                    }
                }
                const h1s = Array.from(document.querySelectorAll('h1'));
                for (const h1 of h1s) {
                    const txt = (h1.innerText || '').trim();
                    if (!txt || txt === 'Documents') continue;
                    return txt;
                }
                return '';
            }
        """)
    except Exception:
        return ""


async def dump_page_diagnostics(page, label: str):
    """Print debugging info about what the page currently shows.  Used on timeouts."""
    try:
        info = await page.evaluate("""
            () => ({
                title: document.title,
                url:   location.href,
                h1s:   Array.from(document.querySelectorAll('h1')).map(h => (h.innerText || '').trim()),
                hasInvokeAtlas: typeof window.invokeAtlas,
                totalItemsText: (() => {
                    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    while (w.nextNode()) {
                        const m = w.currentNode.textContent.match(/Total Items:\\s*\\d+/);
                        if (m) return m[0];
                    }
                    return '(none)';
                })()
            })
        """)
        print(f"      [diag {label}] title={info.get('title')!r}")
        print(f"      [diag {label}] url={info.get('url')}")
        print(f"      [diag {label}] h1s={info.get('h1s')}")
        print(f"      [diag {label}] window.invokeAtlas is {info.get('hasInvokeAtlas')}")
        print(f"      [diag {label}] {info.get('totalItemsText')}")
    except Exception as e:
        print(f"      [diag {label}] (failed to read: {e})")


async def read_total_items(page) -> int:
    """Read 'Total Items: N' from anywhere on the page.  -1 if not present."""
    try:
        return await page.evaluate("""
            () => {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                while (walker.nextNode()) {
                    const m = walker.currentNode.textContent.match(/Total Items:\\s*(\\d+)/);
                    if (m) return parseInt(m[1]);
                }
                return -1;
            }
        """)
    except Exception:
        return -1


async def read_page_size(page) -> int:
    """
    Read the 'Show: 100' dropdown value.  The Show dropdown always contains
    100 (or 50/25) as an option — the pager dropdown contains 1,2,3... so
    we explicitly avoid matching it.
    """
    try:
        return await page.evaluate("""
            () => {
                for (const sel of document.querySelectorAll('select')) {
                    const vals = Array.from(sel.options)
                        .map(o => parseInt(o.value))
                        .filter(v => !isNaN(v));
                    if (vals.includes(100) || vals.includes(50)) {
                        return parseInt(sel.value) || 100;
                    }
                }
                return 100;
            }
        """)
    except Exception:
        return 100


async def read_first_row_id(page) -> str:
    """
    Return the GUID of the first real content row in the grid.  Used as a
    change-detection signal when paginating.
    """
    try:
        return await page.evaluate("""
            () => {
                const g = document.querySelector('#nodeGrid_gridView');
                if (!g) return '';
                for (const row of g.querySelectorAll('tr')) {
                    if (!row.querySelector('input[type="checkbox"]')) continue;
                    if (row.querySelectorAll('td').length < 4)        continue;

                    const fl = row.querySelector('a[onclick*="NavigateToFolder"]');
                    if (fl) {
                        const m = (fl.getAttribute('onclick') || '')
                            .match(/'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})'/i);
                        if (m) return m[1];
                    }
                    const dl = row.querySelector('a[href*="/atlas/Documents/View"]');
                    if (dl) {
                        const m = (dl.getAttribute('href') || '')
                            .match(/[?&]Id=([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/i);
                        if (m) return m[1];
                    }
                }
                return '';
            }
        """)
    except Exception:
        return ""


# ═════════════════════════════════════════════════════════════════════════════
# Waiters — these poll the readers until some condition holds
# ═════════════════════════════════════════════════════════════════════════════

async def wait_for_stable_total_items(page, timeout_sec: int) -> int:
    """
    Poll Total Items until two consecutive reads POLL_INTERVAL_SEC apart
    return the same value.  Returns -1 if it never stabilised within timeout.
    """
    prev = -2
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        cur = await read_total_items(page)
        if cur >= 0 and cur == prev:
            return cur
        prev = cur
        await asyncio.sleep(POLL_INTERVAL_SEC)
    return -1


async def wait_for_header_change(page, previous_header: str, timeout_sec: int,
                                 expected_name: str = "") -> str:
    """
    Poll until the page header reflects a new folder.

    If expected_name is given, waits for the header to MATCH it via _names_match
    (loose, prefix-tolerant, normalised).  Otherwise, waits for the header to
    simply differ from previous_header.

    Returns the new header on success, '' on timeout.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        current = await read_folder_header(page)
        if current:
            if expected_name:
                if _names_match(current, expected_name):
                    return current
            else:
                if current != previous_header:
                    return current
        await asyncio.sleep(POLL_INTERVAL_SEC)
    return ""


async def wait_for_first_row_change(page, previous_first_id: str, timeout_sec: int) -> bool:
    """Poll until the first row's GUID differs from previous_first_id."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        current = await read_first_row_id(page)
        if current and current != previous_first_id:
            return True
        await asyncio.sleep(POLL_INTERVAL_SEC)
    return False


# ═════════════════════════════════════════════════════════════════════════════
# Navigation
# ═════════════════════════════════════════════════════════════════════════════

async def _try_invoke_atlas(page, folder_id: str) -> str:
    """
    Try to call invokeAtlas in the page.  Returns '' on success, error string
    on failure.  Tries window.invokeAtlas first, then bare invokeAtlas.
    """
    js = f"""
        () => {{
            try {{
                if (typeof window.invokeAtlas === 'function') {{
                    window.invokeAtlas('nodeGrid', '{folder_id}', 'NavigateToFolder', null, null, null);
                    return 'window';
                }}
                if (typeof invokeAtlas === 'function') {{
                    invokeAtlas('nodeGrid', '{folder_id}', 'NavigateToFolder', null, null, null);
                    return 'bare';
                }}
                return 'NOT_DEFINED';
            }} catch (e) {{
                return 'ERROR: ' + (e && e.message || String(e));
            }}
        }}
    """
    try:
        result = await page.evaluate(js)
        if result in ("window", "bare"):
            return ""  # success
        return result  # 'NOT_DEFINED' or 'ERROR: ...'
    except Exception as e:
        return f"EVAL_FAILED: {e}"


async def _try_click_folder_link(page, folder_id: str) -> bool:
    """
    Try to click the folder's link in the currently displayed grid.
    Returns True if a clickable link was found and clicked, False otherwise.
    Only works when the row is on the currently visible page.
    """
    try:
        link = await page.query_selector(
            f'a[onclick*="{folder_id}"][onclick*="NavigateToFolder"]'
        )
        if not link:
            return False
        await link.scroll_into_view_if_needed()
        await link.click()
        return True
    except Exception:
        return False


async def navigate_to_folder(page, folder_id: str, expected_name: str = "") -> bool:
    """
    Navigate into a folder and wait for the page to actually reflect it.

    Strategy:
      1. Short-circuit if we're already there.
      2. Try window.invokeAtlas (preferred — works regardless of which page
         the folder's row lives on).
      3. If invokeAtlas isn't defined OR the title doesn't change within a
         short window, fall back to clicking the row link in the current grid.
      4. Wait for the title to match expected_name (or to differ from before).
      5. Wait for Total Items to stabilise.

    Returns True on success, False on timeout (with diagnostics dumped).
    """
    label = expected_name or folder_id[:8]
    try:
        before_header = await read_folder_header(page)

        # Step 1: short-circuit
        if expected_name and _names_match(before_header, expected_name):
            return True

        # Step 2: try invokeAtlas
        invoke_err = await _try_invoke_atlas(page, folder_id)
        used_method = "invokeAtlas"

        if invoke_err:
            print(f"      ℹ️  invokeAtlas unusable ({invoke_err}); trying row click for {label}")
            clicked = await _try_click_folder_link(page, folder_id)
            if not clicked:
                print(f"      ⚠️  Could not find row link for {label} on current page")
                await dump_page_diagnostics(page, "navigate-no-link")
                return False
            used_method = "row-click"
        else:
            quick_change = await wait_for_header_change(page, before_header, 5)
            if not quick_change:
                print(f"      ℹ️  invokeAtlas had no effect on {label} after 5s; trying row click")
                clicked = await _try_click_folder_link(page, folder_id)
                if clicked:
                    used_method = "row-click (fallback)"
                else:
                    print(f"      ℹ️  No row link visible; will keep waiting on invokeAtlas")
            else:
                pass

        # Step 3: wait for title to match expected (or to differ)
        new_header = await wait_for_header_change(
            page, before_header, FOLDER_NAV_TIMEOUT_SEC, expected_name=expected_name
        )
        if not new_header:
            current = await read_folder_header(page)
            print(f"      ⚠️  Header never reached {label} (still '{current}', method={used_method})")
            await dump_page_diagnostics(page, f"navigate-timeout-{label[:20]}")
            return False

        # Step 4: wait for Total Items to stabilise
        total = await wait_for_stable_total_items(page, FOLDER_NAV_TIMEOUT_SEC)
        if total < 0:
            print(f"      ⚠️  Total Items never stabilised in {label}")
            await dump_page_diagnostics(page, f"stable-timeout-{label[:20]}")
            return False

        return True

    except Exception as e:
        print(f"      ⚠️  navigate_to_folder error for {label}: {e}")
        return False


async def navigate_to_page(page, page_num: int) -> bool:
    """
    Drive the Page dropdown to page_num AND wait for the first row to change.
    Adds the option to the select if missing (CLM renders pager options
    lazily — only ~13 options appear even for a 22-page folder).
    """
    try:
        before_first_id = await read_first_row_id(page)

        ok = await page.evaluate(f"""
            () => {{
                const allSels = Array.from(document.querySelectorAll('select'));
                const pager = allSels.find(sel =>
                    sel.options.length > 0 && parseInt(sel.options[0].value) === 1
                );
                if (!pager) return false;
                let opt = Array.from(pager.options).find(o => parseInt(o.value) === {page_num});
                if (!opt) {{
                    opt = document.createElement('option');
                    opt.value = String({page_num});
                    opt.text  = String({page_num});
                    pager.appendChild(opt);
                }}
                pager.value = String({page_num});
                pager.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return true;
            }}
        """)
        if not ok:
            print(f"      ⚠️  Pager dropdown not found")
            return False

        changed = await wait_for_first_row_change(page, before_first_id, PAGE_NAV_TIMEOUT_SEC)
        if not changed:
            print(f"      ⚠️  First row never changed after jumping to page {page_num}")
            return False

        await asyncio.sleep(1.0 * SLOW_MODE)
        return True

    except Exception as e:
        print(f"      ⚠️  navigate_to_page error: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
# Grid reading
# ═════════════════════════════════════════════════════════════════════════════

async def get_grid_rows_on_current_page(page) -> list:
    """
    Return a list of {name, id, type} dicts for every real content row on
    the currently displayed page.
    """
    try:
        rows = await page.evaluate("""
            () => {
                const results = [];
                const seen    = new Set();
                const gridView = document.querySelector('#nodeGrid_gridView');
                if (!gridView) return results;

                gridView.querySelectorAll('tr').forEach(row => {
                    if (!row.querySelector('input[type="checkbox"]')) return;
                    if (row.querySelectorAll('td').length < 4)        return;

                    row.querySelectorAll('a[onclick*="NavigateToFolder"]').forEach(link => {
                        const onclick = link.getAttribute('onclick') || '';
                        const name    = link.innerText.trim();
                        if (!name) return;
                        const m = onclick.match(/'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})'/i);
                        if (!m || seen.has(m[1])) return;
                        seen.add(m[1]);
                        results.push({ name, id: m[1], type: 'folder' });
                    });

                    row.querySelectorAll('a[href*="/atlas/Documents/View"]').forEach(link => {
                        const href = link.getAttribute('href') || '';
                        const name = link.innerText.trim() || link.title || '';
                        if (!name) return;
                        const m = href.match(/[?&]Id=([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/i);
                        if (!m || seen.has(m[1])) return;
                        seen.add(m[1]);
                        results.push({ name, id: m[1], type: 'document' });
                    });
                });

                return results;
            }
        """)
        return rows
    except Exception as e:
        print(f"      ⚠️  Could not read grid: {e}")
        return []


async def get_all_grid_rows(page) -> list:
    """
    Walk every page of the currently displayed folder and collect all rows.
    Assumes the caller has already navigated to the folder and waited for it
    to load (i.e. navigate_to_folder has returned True).
    """
    total_items = await wait_for_stable_total_items(page, FOLDER_NAV_TIMEOUT_SEC)
    if total_items < 0:
        print(f"      ⚠️  Could not read Total Items — assuming single page")
        total_items = 0

    page_size   = max(1, await read_page_size(page))
    total_pages = max(1, math.ceil(total_items / page_size)) if total_items > 0 else 1

    print(f"      [pages: {total_pages}, items: {total_items}]")

    all_rows  = []
    seen_ids  = set()

    for pg in range(1, total_pages + 1):
        if pg > 1:
            if not await navigate_to_page(page, pg):
                print(f"      ⚠️  Could not navigate to page {pg} — stopping pagination")
                break

            current_total = await read_total_items(page)
            if current_total != total_items:
                print(f"      ⚠️  Drift on page {pg} "
                      f"(expected {total_items}, got {current_total}) — stopping")
                break

        rows = await get_grid_rows_on_current_page(page)
        for r in rows:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                all_rows.append(r)

    return all_rows


# ═════════════════════════════════════════════════════════════════════════════
# Download
# ═════════════════════════════════════════════════════════════════════════════

async def download_via_url(page, context, doc: dict, dest_folder: Path) -> bool:
    """
    Try to download a document via Docusign CLM's DownloadRedirect endpoint.

    This is the primary download path — far simpler and more reliable than
    interacting with the row's submenu.  Tries Type=DocumentDownload first
    (preserves the original file format), then Type=DocumentDownloadAsPdf
    as a fallback.

    Returns True on success, False if both URL types fail (in which case
    the caller should fall back to the UI click flow).
    """
    doc_id   = doc.get("id", "")
    filename = sanitise(doc.get("name", doc_id))
    if not filename or not doc_id:
        return False

    for dl_type in ("DocumentDownload", "DocumentDownloadAsPdf"):
        url = f"{DOWNLOAD_API_URL}?aid={AID}&Type={dl_type}&Id={doc_id}"
        try:
            resp = await context.request.get(url, timeout=60000)
            if not resp.ok:
                continue

            ct = (resp.headers.get("content-type", "") or "").lower()
            if "text/html" in ct:
                continue

            body = await resp.body()
            if not body or len(body) < 32:
                continue

            cd = resp.headers.get("content-disposition", "") or ""
            actual_name = filename
            m = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)', cd)
            if m:
                cd_name = sanitise(m.group(1).strip().strip('"'))
                if cd_name:
                    actual_name = cd_name

            if dl_type == "DocumentDownloadAsPdf" and not actual_name.lower().endswith(".pdf"):
                actual_name = actual_name + ".pdf"

            save_path = dest_folder / actual_name
            save_path.write_bytes(body)
            print(f"      ✅ {save_path.name}  ({dl_type}, {len(body):,} bytes)")
            return True

        except Exception as e:
            print(f"        [debug] {dl_type} fetch error: {e}")
            continue

    return False


async def download_document(page, context, doc: dict, dest_folder: Path):
    """
    Top-level download orchestrator.  Tries the direct URL approach first
    (fast and reliable), then falls back to the UI click flow if that fails.
    """
    global downloaded, skipped, failed

    doc_id   = doc.get("id", "")
    filename = sanitise(doc.get("name", doc_id))
    if not filename or not doc_id:
        return

    if doc_id in completed_docs:
        skipped += 1
        print(f"      ⏭  Already done: {filename}")
        return

    # Primary: direct URL fetch
    try:
        if await download_via_url(page, context, doc, dest_folder):
            downloaded += 1
            mark_doc_done(doc_id)
            return
    except SessionExpiredError:
        raise
    except Exception as e:
        print(f"        [debug] download_via_url raised: {e}")

    await check_session_or_raise(page)

    # Fallback: UI click flow
    print(f"      ↩  URL fetch failed, falling back to UI click for: {filename}")
    pre_count = downloaded
    await download_document_by_click(page, context, doc, dest_folder)
    if downloaded > pre_count:
        mark_doc_done(doc_id)


async def download_document_by_click(page, context, doc: dict, dest_folder: Path):
    """
    Download a single document via the UI click flow:
      1. Hover its row to reveal the ▼ action button
      2. Click ▼ to open the submenu
      3. Inspect the menu — if 'Download' is absent, log for manual review
      4. Click Download and wait for either a direct download or a new tab
    """
    global downloaded, skipped, failed

    doc_id   = doc.get("id", "")
    filename = sanitise(doc.get("name", doc_id))
    if not filename or not doc_id:
        return

    async def fresh_row():
        return await page.query_selector(f'tr:has(a[href*="Id={doc_id}"])')

    try:
        # Step 1: hover to reveal the action button
        row = await fresh_row()
        if not row:
            print(f"      ⚠️  Row not found: {filename}")
            failed += 1
            failed_docs.append((str(dest_folder), filename))
            return
        await row.hover()
        await asyncio.sleep(1.0 * SLOW_MODE)

        # Step 2: re-query row and click the ▼ dropdown trigger
        click_result = await page.evaluate("""
            (docId) => {
                const links = document.querySelectorAll('a[href*="Id=' + docId + '"]');
                let row = null;
                for (const link of links) {
                    const tr = link.closest('tr');
                    if (tr && tr.querySelector('input[type="checkbox"]')) {
                        row = tr;
                        break;
                    }
                }
                if (!row) return { ok: false, reason: 'no_row' };

                const candidates = [
                    'input[type="button"]',
                    '.actionButton',
                    'a.actionButton',
                    'div.actionButton',
                    'img.actionButton',
                    '[class*="actionButton"]',
                    '[class*="ActionButton"]',
                    'a[onclick*="showActionMenu"]',
                    'a[onclick*="actionMenu"]',
                    'a[onclick*="ShowMenu"]',
                    'img[onclick*="actionMenu"]',
                    'img[src*="arrow"]',
                    'img[src*="dropdown"]',
                ];

                for (const sel of candidates) {
                    const el = row.querySelector(sel);
                    if (el) {
                        try {
                            el.click();
                            return { ok: true, selector: sel };
                        } catch (e) {
                        }
                    }
                }

                const snippet = (row.outerHTML || '').slice(0, 1200);
                return { ok: false, reason: 'no_match', html: snippet };
            }
        """, doc_id)

        if not click_result.get("ok"):
            reason = click_result.get("reason", "unknown")
            if reason == "no_row":
                print(f"      ⚠️  Row gone before click: {filename}")
            else:
                print(f"      ⚠️  No ▼ trigger found in row: {filename}")
                html = click_result.get("html", "")
                if html:
                    preview = html.replace("\n", " ")[:500]
                    print(f"         [row-html] {preview}")
            failed += 1
            failed_docs.append((str(dest_folder), filename))
            return

        await asyncio.sleep(1.5 * SLOW_MODE)

        # Step 3: inspect menu items — is Download present?
        menu_items = await page.evaluate("""
            () => {
                const items = [];
                const lis = document.querySelectorAll(
                    'div.actionButton.actionbar ul.actionbar li, ul.actionbar li'
                );
                lis.forEach(li => {
                    const t = li.innerText.trim();
                    if (t) items.push(t);
                });
                return items;
            }
        """)

        has_download = any(re.search(r'download', item, re.IGNORECASE) for item in menu_items)

        if not has_download:
            print(f"      📝 No Download in menu — flagged for manual review: {filename}")
            if menu_items:
                print(f"         (menu contained: {', '.join(menu_items[:6])})")
            needs_manual_review.append((str(dest_folder), filename))
            try:
                await page.mouse.click(10, 10)
            except Exception:
                pass
            return

        # Step 4: find and click the Download item
        dl_locator = None
        for pattern in [r"^Download$", r"^Download a file$", r"Download"]:
            for tag in ["li", "a"]:
                loc = page.locator(tag, has_text=re.compile(pattern, re.IGNORECASE)).first
                try:
                    await loc.wait_for(state="visible", timeout=2000)
                    dl_locator = loc
                    break
                except Exception:
                    pass
            if dl_locator:
                break

        if not dl_locator:
            print(f"      ❌ Download item not clickable: {filename}")
            failed += 1
            failed_docs.append((str(dest_folder), filename))
            return

        dl_result = [None]
        pg_result = [None]

        def on_dl(dl):  dl_result[0] = dl
        def on_pg(pg):  pg_result[0] = pg

        page.once("download", on_dl)
        context.once("page",  on_pg)

        await dl_locator.click()

        poll_iters = int(DOWNLOAD_TIMEOUT_SEC / POLL_INTERVAL_SEC)
        for _ in range(poll_iters):
            if dl_result[0] is not None or pg_result[0] is not None:
                break
            await asyncio.sleep(POLL_INTERVAL_SEC)

        try: page.remove_listener("download", on_dl)
        except Exception: pass
        try: context.remove_listener("page", on_pg)
        except Exception: pass

        # ── Case A: direct browser download ──────────────────────────────
        if dl_result[0] is not None:
            dl: Download = dl_result[0]
            fname     = sanitise(dl.suggested_filename or filename)
            save_path = dest_folder / fname
            await dl.save_as(save_path)
            print(f"      ✅ {save_path.name}")
            downloaded += 1
            return

        # ── Case B: new tab opened ───────────────────────────────────────
        if pg_result[0] is not None:
            new_tab: Page = pg_result[0]
            tab_dl = [None]

            def on_tab_dl(dl): tab_dl[0] = dl
            new_tab.once("download", on_tab_dl)

            try:
                await new_tab.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass

            for _ in range(30):
                if tab_dl[0] is not None:
                    break
                await asyncio.sleep(POLL_INTERVAL_SEC)

            try: new_tab.remove_listener("download", on_tab_dl)
            except Exception: pass

            if tab_dl[0] is not None:
                dl: Download = tab_dl[0]
                fname     = sanitise(dl.suggested_filename or filename)
                save_path = dest_folder / fname
                await dl.save_as(save_path)
                print(f"      ✅ {save_path.name}  (new tab)")
                downloaded += 1
                try: await new_tab.close()
                except Exception: pass
                return

            try: await new_tab.close()
            except Exception: pass

        # Neither fired
        print(f"      ❌ Download timed out: {filename}")
        failed += 1
        failed_docs.append((str(dest_folder), filename))

    except Exception as e:
        print(f"      ❌ Error: {filename} — {e}")
        failed += 1
        failed_docs.append((str(dest_folder), filename))


# ═════════════════════════════════════════════════════════════════════════════
# Folder processing (recursive)
# ═════════════════════════════════════════════════════════════════════════════

async def process_folder(page, context, folder: dict, local_path: Path,
                         parent_id: str, depth: int = 0):
    global visited

    folder_id        = folder.get("id", "")
    folder_name_raw  = folder.get("name", "unknown")
    folder_name_safe = sanitise(folder_name_raw)

    if folder_id in completed_folders:
        return

    if folder_id in visited:
        return
    if depth > MAX_DEPTH:
        return

    visited.add(folder_id)
    folder_path = local_path / folder_name_safe
    folder_path.mkdir(parents=True, exist_ok=True)

    indent = "  " * depth
    print(f"\n{indent}📁 {folder_name_safe}")

    if not await navigate_to_folder(page, folder_id, folder_name_raw):
        await check_session_or_raise(page)
        print(f"{indent}   ⚠️  Could not navigate into folder — skipping")
        return

    rows      = await get_all_grid_rows(page)
    folders   = [r for r in rows if r["type"] == "folder"]
    documents = [r for r in rows if r["type"] == "document"]

    print(f"{indent}   📄 {len(documents)} doc(s)   📁 {len(folders)} subfolder(s)")

    all_subfolders_ok = True

    for doc in documents:
        await download_document(page, context, doc, folder_path)

    for subfolder in folders:
        await process_folder(page, context, subfolder, folder_path, folder_id, depth + 1)
        if not await navigate_to_folder(page, folder_id, folder_name_raw):
            await check_session_or_raise(page)
            print(f"{indent}   ⚠️  Could not return to {folder_name_safe} — stopping its subfolders")
            all_subfolders_ok = False
            break

    if all_subfolders_ok:
        mark_folder_done(folder_id)


# ═════════════════════════════════════════════════════════════════════════════
# Config validation + main
# ═════════════════════════════════════════════════════════════════════════════

def validate_config():
    """Check that required config values are set; print friendly error if not."""
    missing = []
    if not AID:
        missing.append("CLM_AID (or edit AID in the script)")
    if not ROOT_FOLDER_ID:
        missing.append("CLM_ROOT_FOLDER_ID (or edit ROOT_FOLDER_ID in the script)")
    if not CLM_HOST:
        missing.append("CLM_HOST (or edit CLM_HOST in the script)")

    if missing:
        print("=" * 60)
        print("❌ Missing required configuration:")
        print("=" * 60)
        for m in missing:
            print(f"   • {m}")
        print()
        print("See the README for instructions on finding these values.")
        print("Set them at the top of this script, or as environment variables:")
        print()
        print("   export CLM_HOST=na11.springcm.com")
        print("   export CLM_AID=12345")
        print("   export CLM_ROOT_FOLDER_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        print()
        return False
    return True


async def main():
    if not validate_config():
        return

    print("=" * 60)
    print(f"  Docusign CLM Backup — {ROOT_FOLDER_NAME}")
    print(f"  Host: {CLM_HOST}   AID: {AID}")
    print(f"  SLOW_MODE = {SLOW_MODE}")
    print("=" * 60)

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    root_path = DOWNLOAD_DIR / sanitise(ROOT_FOLDER_NAME)
    root_path.mkdir(exist_ok=True)

    load_checkpoints()

    start = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        print(f"\n🌐 Opening Docusign CLM...")
        print(f"👉 1. Log in")
        print(f"   2. Navigate to your root folder ({ROOT_FOLDER_NAME})")
        print(f"   3. Press Enter here when you're on it\n")

        await page.goto(SPRINGCM_URL)

        for _ in range(LOGIN_WAIT_SEC * 2):
            if "springcm.com" in page.url:
                try:
                    await page.wait_for_selector('a[onclick*="invokeAtlas"]', timeout=5000)
                    break
                except Exception:
                    pass
            await asyncio.sleep(0.5)

        print(f"⏸️  Press Enter when on the {ROOT_FOLDER_NAME} folder page...")
        await asyncio.get_event_loop().run_in_executor(None, input)

        total = await wait_for_stable_total_items(page, FOLDER_NAV_TIMEOUT_SEC)
        print(f"\n📂 {ROOT_FOLDER_NAME} folder loaded — Total Items: {total}")

        visited.add(ROOT_FOLDER_ID)

        rows    = await get_all_grid_rows(page)
        folders = [r for r in rows if r["type"] == "folder"]
        docs    = [r for r in rows if r["type"] == "document"]

        print(f"\n📂 Found {len(folders)} subfolders, {len(docs)} loose documents")
        if completed_folders:
            remaining = sum(1 for f in folders if f["id"] not in completed_folders)
            print(f"   ({len(folders) - remaining} already done, {remaining} to go)")
        print(f"📥 Saving to: {root_path}\n")

        if not folders and not docs:
            print(f"⚠️  Nothing found. Make sure you are on the {ROOT_FOLDER_NAME} page.")
            await browser.close()
            return

        # Loose docs directly in root
        for doc in docs:
            try:
                await download_document(page, context, doc, root_path)
            except SessionExpiredError:
                if not await wait_for_reauth(page):
                    await browser.close()
                    return

        # Each subfolder, with session-expiry retry
        for i, folder in enumerate(folders, 1):
            if folder["id"] in completed_folders:
                continue

            print(f"\n[{i}/{len(folders)}]", end="")

            while True:
                try:
                    await process_folder(
                        page, context, folder, root_path,
                        ROOT_FOLDER_ID, 0
                    )
                    break
                except SessionExpiredError:
                    if not await wait_for_reauth(page):
                        await browser.close()
                        elapsed = time.time() - start
                        print(f"\n⏸  Stopped after {elapsed/60:.1f} min — "
                              f"{downloaded} downloaded, {skipped} skipped")
                        print(f"   Re-run the script to resume.")
                        return
                    continue

            try:
                if not await navigate_to_folder(page, ROOT_FOLDER_ID, ROOT_FOLDER_NAME):
                    await check_session_or_raise(page)
                    print(f"⚠️  Could not return to {ROOT_FOLDER_NAME} root — stopping")
                    break
            except SessionExpiredError:
                if not await wait_for_reauth(page):
                    break
                if not await navigate_to_folder(page, ROOT_FOLDER_ID, ROOT_FOLDER_NAME):
                    print(f"⚠️  Still can't get back to {ROOT_FOLDER_NAME} — stopping")
                    break

        await browser.close()

    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"✅ Backup complete in {elapsed/60:.1f} minutes")
    print(f"   Downloaded: {downloaded}")
    print(f"   Skipped:    {skipped}")
    print(f"   Failed:     {failed}")
    print(f"   Manual:     {len(needs_manual_review)}")
    print(f"📂 Files saved to: {root_path}")

    if failed_docs:
        print(f"\n{'='*60}")
        print(f"❌ FAILED DOWNLOADS ({len(failed_docs)}):")
        print(f"{'='*60}")
        for folder_path, fname in failed_docs:
            try:
                rel = str(Path(folder_path).relative_to(root_path))
            except Exception:
                rel = folder_path
            print(f"  {rel} / {fname}")
        report_path = DOWNLOAD_DIR / "failed_downloads.txt"
        with open(report_path, "w") as f:
            f.write(f"Failed downloads — {time.strftime('%Y-%m-%d %H:%M')}\n")
            f.write("=" * 60 + "\n")
            for folder_path, fname in failed_docs:
                f.write(f"{folder_path} / {fname}\n")
        print(f"\n  (also saved to {report_path})")

    if needs_manual_review:
        print(f"\n{'='*60}")
        print(f"📝 NEEDS MANUAL REVIEW ({len(needs_manual_review)}):")
        print(f"    (no Download option in submenu — likely Docusign-only)")
        print(f"{'='*60}")
        for folder_path, fname in needs_manual_review:
            try:
                rel = str(Path(folder_path).relative_to(root_path))
            except Exception:
                rel = folder_path
            print(f"  {rel} / {fname}")
        report_path = DOWNLOAD_DIR / "needs_manual_review.txt"
        with open(report_path, "w") as f:
            f.write(f"Needs manual review — {time.strftime('%Y-%m-%d %H:%M')}\n")
            f.write("These documents had no 'Download' item in their submenu.\n")
            f.write("Open each one in the Docusign CLM UI and download via Docusign.\n")
            f.write("=" * 60 + "\n")
            for folder_path, fname in needs_manual_review:
                f.write(f"{folder_path} / {fname}\n")
        print(f"\n  (also saved to {report_path})")


if __name__ == "__main__":
    asyncio.run(main())
