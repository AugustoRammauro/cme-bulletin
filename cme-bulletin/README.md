# CME Daily Bulletin → Google Sheet

Reads the CME Group Daily Information Bulletin each weekday and records one row
per instrument. Runs on GitHub's servers, so nothing needs to be switched on at
your end.

Tracked: **GC** (gold), **SI** (silver), **CL** (crude), **ES** (S&P 500),
**NQ** (Nasdaq), **YM** (Dow).

---

# Setup

Two steps, about five minutes, no credentials and no Google Cloud.

The job writes its results to a plain text file called `data/history.csv` inside
a GitHub repository. Your spreadsheet then reads that file with a single
formula. GitHub does the work on a schedule; your sheet just looks at the
answer.

## Step 1 — put this folder on GitHub

1. Go to **[github.com/new](https://github.com/new)**.
2. **Repository name:** `cme-bulletin`.
3. Select **Public**. This matters — the sheet reads the file over a plain
   web address, which only works on a public repository. Nothing private is
   involved; it is published CME data and no passwords or keys are stored
   anywhere in this project.
4. Click **Create repository**.
5. On the next page click **uploading an existing file**.
6. Unzip `cme-bulletin.zip`, then drag *everything inside* the folder into the
   browser window — including the hidden `.github` folder, which holds the
   schedule. If your file manager hides it, turn on "show hidden files" first,
   or use the git commands at the bottom of this file instead.
7. Click **Commit changes**.

## Step 2 — connect your spreadsheet

1. In your spreadsheet, make a **new tab** (the **+** at the bottom left).
   Name it `Bulletin`.
2. Click cell **A1** and paste this, replacing `YOUR-USERNAME` with your
   GitHub username:

   ```
   =IMPORTDATA("https://raw.githubusercontent.com/YOUR-USERNAME/cme-bulletin/main/data/history.csv")
   ```

3. Press Enter. The table fills in by itself — headers and all — starting with
   the 25 August rows already included.

That's the whole setup. From here the file grows by six rows each trading day
and the tab follows it automatically.

## Check it works

In your repository, open the **Actions** tab. If GitHub asks you to enable
workflows, click the green button. Then choose **CME daily bulletin** on the
left and **Run workflow** on the right.

Give it a minute, then click into the run to see the log. It prints every value
it read before recording anything. If the bulletin is not FINAL yet it says so
and stops — that is a pass, not a failure.

## Things worth knowing

- **The tab is read-only.** Everything under A1 is produced by that one
  formula, so don't type into those cells — the next refresh would overwrite
  it. Put your own calculations in columns to the right, or on another tab
  pointing at this one.
- **It refreshes about once an hour.** Google decides when, not us. To force
  it, delete the formula, press Enter, then undo with Ctrl+Z.
- **Your existing tab is untouched.** This lands on the new `Bulletin` tab.
  Once you're happy with it, you can point your old columns at the new tab or
  retire them.

---

# What each column means

| Column | Source |
|---|---|
| Instrument | fixed |
| Date | bulletin header trade date |
| Volume (total) | product TOTAL row, Globex volume column |
| Open Interest (total) | product TOTAL row |
| Open Interest change | product TOTAL row |
| Settlement Price | highest-open-interest contract month |
| Settlement Change | same contract |
| Settlement % Chg | derived, in percent units (`-3.12` means −3.12%) |
| Delta RTH | `(settlement − session open) / session open`, same units |
| Signal | Bullish / Sideways / Bearish, see below |
| Contract | which month the settlement came from |
| Session Open | the open Delta RTH was measured against |

The last two exist so any settlement or Delta RTH figure can be traced back to
the exact contract month and opening price it came from.

## Delta RTH — one rule for all six

The open is the first price CME publishes for that product's own trading
session. Sections 61 and 62 label that column `GLOBEX OPEN`. Section 11 labels
the E-mini S&P block `PIT OPEN RANGE`, but that label is left over from the
open-outcry era: the S&P pit closed years ago, and the block's volume is
plainly whole-session — 1,002,413 lots against 2,045,669 open interest, and
2.05× the Nasdaq's volume, which is the normal full-session ES:NQ ratio. A
genuine pit-only figure would be near zero. So all six instruments are measured
the same way.

Two honest caveats. This is **not** order-flow delta (aggressive buys minus
sells) — the bulletin does not carry that. And because the session open is the
electronic open, this measures the whole ~23-hour session rather than the RTH
cash session. It still differs usefully from Settlement % Chg, by exactly the
gap between the prior settlement and the session open: on 25 August that gap
was 0.02pp for CL but 0.57pp for SI.

## Signal

```
|Settlement % Chg| < 0.30%           → Sideways        (noise)
Delta RTH disagrees with the move    → Sideways        (gapped overnight)
|Settlement % Chg| ≥ 1.50%           → Bullish/Bearish (decisive on its own)
otherwise, open interest rose        → Bullish/Bearish (new money)
otherwise                            → Sideways        (covering/liquidation)
```

Rising open interest means new positions are being opened — someone is taking a
side. Falling open interest means positions are being closed, so a rally is
short covering and a break is long liquidation; neither is a strong directional
statement. The 1.50% override exists because a move that large is information
regardless of what open interest did.

Thresholds are `QUIET_PCT` and `STRONG_PCT` in `cme_bulletin.py`.

---

# Schedule

`0,15,30,45 15-20 * * 1-5` (UTC) — every 15 minutes, Monday to Friday.

GitHub's cron is UTC-only and can fire minutes late under load, so instead of
betting on one exact firing the job runs across a window covering 10:15 CT in
both CST and CDT. It exits immediately if it's before 10:15 CT, if the bulletin
still says PRELIMINARY, or if the date is already recorded — so the first run
that finds FINAL data writes the rows and every run after that is a no-op
costing a few seconds.

To change the target time, edit `RELEASE_HOUR` / `RELEASE_MIN` in
`run_daily.py` and widen the cron window in
`.github/workflows/daily-bulletin.yml` to match.

# Safety checks

The job is unattended, so it refuses to write rather than write something wrong.

- **FINAL required.** Preliminary figures differ from final ones — on 25 August
  the preliminary CL open interest was 1,907,297 against a final 1,906,740. The
  header is checked on every section.
- **Cache-busting.** The bulletin at `/current/` is overwritten in place, so a
  cached PRELIMINARY copy could otherwise be served after FINAL is published.
  Each request carries `Cache-Control: no-cache` and a unique query string.
- **Cross-section date agreement.** All three sections must report the same
  trade date, which catches one stale section.
- **Staleness ceiling.** A bulletin more than 5 days old is refused outright —
  this is what stops a cached yesterday-FINAL from being filed as today.
- **Idempotent.** An instrument/date already recorded is never rewritten, so
  the 15-minute retries cannot produce duplicates.
- **Concurrency group.** Two runs can never write at once.

# Known limitations

- **Volume excludes PNT/pit.** The Globex column only, matching your existing
  history. Separately-listed privately-negotiated volume was 22,149 lots for CL
  on 25 August, 2.9% of the total.
- **Signal ignores volume.** A volume reading needs a baseline (say a 20-day
  average) to mean anything. `data/history.csv` is accumulating one; once
  there are a few weeks of rows, a volume term can be added.
- **GitHub disables cron on idle repos.** Schedules stop after 60 days without
  repository activity. The job commits `data/history.csv` on every day it
  records rows, which keeps the repo active — but if you pause it for two
  months, re-enable it in the Actions tab.

---

# For reference

## Running it on your own machine

```bash
pip install -r requirements.txt

# parse the bundled 25 Aug 2026 bulletins, write nothing
python run_daily.py --dry-run --pdf-dir samples

# download today's and print, without writing
python run_daily.py --dry-run --ignore-clock

python -m pytest test_pipeline.py -q
```

## Uploading with git instead of the browser

```bash
cd cme-bulletin
git init && git add -A && git commit -m "CME bulletin extractor"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/cme-bulletin.git
git push -u origin main
```

## Files

```
cme_bulletin.py     PDF parsing, derived measures, signal rule
fetch.py            downloads, with cache-busting
run_daily.py        orchestration, safety guards, CSV writing
sheets.py           OPTIONAL, unused by default - direct Sheets writing
test_pipeline.py    29 tests, including the two parsing hazards below
samples/            FINAL bulletins for 25 Aug 2026, used by the tests
data/history.csv    the file your sheet reads
```

## Two parsing hazards worth knowing about

Both are covered by tests, so if CME changes the template you get a red run
rather than a wrong number.

1. **Section 11 drops the decimal point in the point-change column.** ES's
   +22.25 is printed as `2225` and NQ's +171.00 as `17100`, while YM's prints
   normally as `156.00`. The change is rescaled by the number of decimals shown
   in that contract's own settlement price.
2. **Long values overflow their column.** NQ's settlement of 29276.75 is
   typeset as `29276.7` with the trailing `5` pushed onto the next line,
   right-aligned to the same edge. The parser stitches those fragments back
   together, which is why it works on coordinates rather than on text lines.

## If you later want rows written directly into your existing tab

`sheets.py` does that, but it needs a Google service account and its JSON key
stored as a repository secret. The file's docstring has the details. The
IMPORTDATA route above avoids all of it.
