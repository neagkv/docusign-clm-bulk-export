# Docusign CLM Bulk Backup

A Python script that recursively downloads every document from a folder tree in **Docusign CLM** (formerly **SpringCM**) to your local disk, preserving the folder structure.

## Why this exists

Docusign CLM has no built-in bulk-export feature. The web UI lets you download one document at a time, or zip a few at once — but anything more than a small folder is impractical, and the zip option breaks if any subfolders are present. If you need to migrate off CLM, audit your contracts, or just keep a local archive, you're stuck clicking through thousands of documents by hand.

This script automates that. Point it at a root folder, log in once, and walk away. It downloaded **3,700+ documents across 5,600+ folders** during testing.

## Features

- **Drives a real Chromium browser via Playwright** — so SSO, Okta, MFA, and any other login flow your org uses all work normally. You log in once; the script does the rest.
- **Resume on crash / timeout / Ctrl-C.** Progress is checkpointed to disk after every single document and folder. Re-run the script and it picks up exactly where it left off.
- **Session-expiry detection.** When CLM kicks you out (every few hours), the script pauses, prompts you to log back in, and then continues from the same folder.
- **Per-document downloads, never bulk zips.** Bulk zip downloads break when any subfolder is present in the selection — and they leave you with a giant unsorted archive instead of a clean folder tree.
- **Direct-URL fast path.** Primary download method calls CLM's internal `DownloadRedirect` endpoint, which is much faster and more reliable than driving the UI menu. Falls back to the UI click flow when the URL won't work.
- **GUID-based dedup.** Two documents with long names that truncate to the same 80-char filename will both be saved correctly.
- **Surfaces docs that need human attention.** Some documents (typically Docusign-signed ones) don't expose a Download option in their menu and have to be downloaded individually from inside the document. The script flags these in `needs_manual_review.txt` and keeps going.

## Requirements

- Python 3.9+
- A Docusign CLM / SpringCM account with permission to read the folders you want to back up
- ~30 seconds of attention every few hours when the session expires

## Installation

```bash
git clone https://github.com/neagkv/docusign-clm-bulk-export.git
cd docusign-clm-bulk-export
pip install -r requirements.txt
python -m playwright install chromium
```

## Configuration

You need three values: your CLM hostname, your account ID (`AID`), and the GUID of the folder you want to back up.

### Finding your CLM hostname

Log into Docusign CLM and look at the URL bar. The host is the part before `/atlas/...`:

- US: usually `na11.springcm.com` (most common) or `na21.springcm.com`
- EU: usually `eu11.springcm.com`

### Finding your AID

After you've navigated to any folder in CLM, look at the URL. You'll see something like:

```
https://na11.springcm.com/atlas/d/Documents/BrowseDocuments?aid=12345&folderId=...
```

The number after `aid=` is your account ID.

### Finding the root folder ID

Navigate to the folder you want to back up (the script will recurse into all its subfolders). Look at the URL again:

```
https://na11.springcm.com/atlas/d/Documents/BrowseDocuments?aid=12345&folderId=abcd1234-...&...
```

The GUID after `folderId=` is your root folder ID.

If the URL doesn't show `folderId` (CLM sometimes elides it), open the browser console (F12) and run:

```javascript
window.location.search.match(/folderId=([a-f0-9-]+)/)?.[1]
```

### Setting the values

Either edit the `CONFIG` block at the top of `docusign_clm_backup.py`:

```python
CLM_HOST       = "na11.springcm.com"
AID            = "12345"
ROOT_FOLDER_ID = "abcd1234-5678-90ab-cdef-1234567890ab"
ROOT_FOLDER_NAME = "Contracts"  # cosmetic; used as the local folder name
```

…or set them via environment variables:

```bash
export CLM_HOST=na11.springcm.com
export CLM_AID=12345
export CLM_ROOT_FOLDER_ID=abcd1234-5678-90ab-cdef-1234567890ab
export CLM_ROOT_FOLDER_NAME=Contracts
python docusign_clm_backup.py
```

## Usage

```bash
python docusign_clm_backup.py
```

A Chromium window will open. Then:

1. **Log into Docusign CLM** in that window (using your normal SSO / username+password / MFA).
2. **Navigate to the root folder** you configured (the one whose GUID you put in `ROOT_FOLDER_ID`).
3. **Switch to the terminal** and press Enter.

The script will:

- Read the folder, paginate through all of its items
- Recursively descend into every subfolder
- Download each document as it goes, into `clm_backup/<ROOT_FOLDER_NAME>/...`
- Print a running progress log

You can leave it running. When the session expires (typically every few hours), it'll pause and tell you to log back in — your progress is saved, so even if you walk away for a while, you won't lose anything.

When it finishes, you'll get a summary:

```
✅ Backup complete in 287.4 minutes
   Downloaded: 3742
   Skipped:    8
   Failed:     3
   Manual:     21
📂 Files saved to: /path/to/clm_backup/Contracts
```

## Output structure

```
clm_backup/
├── .completed_docs.txt          ← progress checkpoint (don't delete)
├── .completed_folders.txt       ← progress checkpoint (don't delete)
├── failed_downloads.txt         ← any docs that errored out
├── needs_manual_review.txt      ← docs with no Download option in their menu
└── Contracts/                   ← named after ROOT_FOLDER_NAME
    ├── Subfolder A/
    │   ├── document_1.pdf
    │   └── Sub-subfolder/
    │       └── document_2.docx
    └── Subfolder B/
        └── document_3.pdf
```

## Resuming a partial run

Just run the script again. It'll read `.completed_docs.txt` and `.completed_folders.txt` from the output directory and skip everything that's already done. There's nothing to configure — resume is the default behavior.

If you want to start fresh, delete the two `.completed_*.txt` files (or the entire output directory).

## When something fails

The script tries hard to keep going past individual failures. You'll get two reports at the end if anything didn't work:

**`failed_downloads.txt`** — documents that errored out during download. Usually transient (network blip, session expired mid-download). Re-run the script and it'll retry just those.

**`needs_manual_review.txt`** — documents whose action menu didn't have a Download option. Almost always these are Docusign-signed documents that have to be downloaded from inside the document viewer. Open each one in the CLM UI and download via the Docusign panel that appears.

## Tuning

The `SLOW_MODE` variable at the top of the script is a global multiplier on every wait timeout. The default `1.0` is calibrated for a fast connection and a healthy CLM tenant. If you're seeing lots of timeouts:

```python
SLOW_MODE = 2.0  # twice as patient everywhere
```

The other timeouts (`FOLDER_NAV_TIMEOUT_SEC`, `PAGE_NAV_TIMEOUT_SEC`, `DOWNLOAD_TIMEOUT_SEC`) are derived from `SLOW_MODE`, so you usually only need to adjust this one value.

## Troubleshooting

**"Missing required configuration"** — you didn't set `CLM_HOST`, `AID`, or `ROOT_FOLDER_ID`. See the [Configuration](#configuration) section above.

**"Header never reached \<folder name\>"** — the script tried to navigate into a folder and the page title never updated to match. Usually transient; the folder will be retried on a re-run. If it happens consistently for one folder, check that you actually have read access to that folder in CLM.

**"No ▼ trigger found in row"** — the script couldn't locate the action-menu button on a document row. The script will dump a snippet of the row's HTML so you can see the structure. Different CLM tenants sometimes render this button differently; if you hit this, please [open an issue](#contributing) with the HTML snippet.

**Session keeps expiring quickly** — your CLM tenant has aggressive session timeouts. The script handles this gracefully (pauses and asks you to log back in) but you may want to do this run during a stretch when you can babysit it for a few minutes every hour.

**Script hangs on "Press Enter when on the X folder page"** — that's not a hang, it's waiting for you. Press Enter in the terminal once you've manually navigated to the root folder in the browser.

## Limitations and caveats

- **Read-only.** This script never writes to or modifies anything in your CLM tenant. It only reads metadata (folder/document listings) and downloads files. But you're still automating actions against a system you don't own — make sure your org's policy allows this.
- **Manual login.** No headless or unattended mode. SSO/Okta/MFA are too varied to support reliably without you in the loop. This is a feature, not a bug — it means there are no credentials to leak.
- **CLM UI changes can break it.** Docusign occasionally updates the CLM UI. The script targets specific DOM selectors (`#nodeGrid_gridView`, `invokeAtlas`, etc.) that have been stable for years, but if a redesign lands, the script may break. PRs welcome.
- **Slow.** Expect roughly 1–3 documents per second on average. A 10,000-document tenant will take several hours. Plan accordingly and use `SLOW_MODE` to tune for your environment.

## Contributing

Issues and PRs welcome. If you hit a problem, please include:

- The full console output (with any `[diag ...]` debug lines)
- Your CLM region (na11, na21, eu11, etc.)
- Anything notable about the folder where it failed (very deep nesting? unusual characters in names? Docusign-signed docs?)

## License

MIT — see [LICENSE](LICENSE) for full text.

## Disclaimer

This is an unofficial tool, not affiliated with or endorsed by Docusign. "Docusign", "Docusign CLM", and "SpringCM" are trademarks of their respective owners. Use at your own risk and in accordance with your organization's policies and your CLM license terms.
