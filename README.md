# CME Daily Bulletin → Google Sheet

Reads the CME Group Daily Information Bulletin each weekday and records one row
per instrument. Runs on GitHub's servers, so nothing needs to be switched on at
your end.

Tracked: **GC** (gold), **SI** (silver), **CL** (crude), **ES** (S&P 500),
**NQ** (Nasdaq), **YM** (Dow).

---

# Setup

No credentials, no Google Cloud, no git, and nothing to install.

Every file in this project sits at the top level, so GitHub's web uploader can
take them all in one drag. The single exception is the schedule file, which
GitHub *requires* to live at `.github/workflows/`, and there is a way to create
that in the browser without ever making a folder.

The job writes its results to a plain text file, `history.csv`, in a GitHub
repository. Your spreadsheet reads that file with one formula. GitHub does the
work on a schedule; your sheet just looks at the answer.

## Step 1 — the repository

Go to **[github.com/new](https://github.com/new)**, name it `cme-bulletin`,
select **Public**, and click **Create repository**.

Public matters: your sheet fetches the file over a plain web address, which
only works on a public repo. Nothing private is involved — it is published CME
data, and this project stores no passwords or keys anywhere.

## Step 2 — upload the files

In the repository, click **Add file → Upload files**.

Open the unzipped folder, select **all** the files with **Ctrl+A**, and drag
them into the browser window. Wait until every filename appears in the list,
then **scroll to the bottom and click the green Commit changes button** — files
are not saved until you do.

> If you use the *"choose your files"* link instead of dragging, it opens a
> file picker that can only select files. That is fine here — everything is a
> file. It is also why folders went missing on earlier attempts.

## Step 3 — the schedule file

This is the one file that must live in a folder. Don't try to upload it —
create it, and GitHub will make the folders for you.

1. In the repository, click **Add file → Create new file**.
2. In the filename box at the top, type exactly:

   ```
   .github/workflows/daily-bulletin.yml
   ```

   As you type each `/`, GitHub turns the part before it into a folder. That is
   the trick — you never create a folder yourself.
3. Open `daily-bulletin.yml` from the unzipped folder in Notepad, select all,
   copy, and paste it into the big editor box.
4. Click **Commit changes**.

## Step 4 — connect your spreadsheet

1. In your spreadsheet, make a **new tab** (the **+** at the bottom left) and
   name it `Bulletin`.
2. Click cell **A1** and paste this, replacing `YOUR-USERNAME` with your GitHub
   username:

   ```
   =IMPORTDATA("https://raw.githubusercontent.com/YOUR-USERNAME/cme-bulletin/main/history.csv")
   ```

3. Press Enter. The table fills in by itself — headers and all — starting with
   the 25 August rows already included.

## Step 5 — check it

The repository root should now list `history.csv`, `cme_bulletin.py`,
`fetch.py`, `run_daily.py`, `requirements.txt` and a `.github` folder.

Open the **Actions** tab. If GitHub asks you to enable workflows, click the
green button — without that the schedule stays dormant. Then choose **CME daily
bulletin** and **Run workflow** to test it. The log prints every value it read
before recording anything; if the bulletin isn't FINAL yet it says so and stops,
which is a pass.

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
| Settlement % Chg | derived, as a fraction (`-0.0312` = −3.12%) |
| Delta RTH | `(settlement − session open) / session open`, same units |
| Signal | Bullish / Sideways / Bearish, see below |
| Contract | NinjaTrader notation, e.g. `GC DEC26` |
| Session Open | the open Delta RTH was measured against |

The last two exist so any settlement or Delta RTH figure can be traced back to
the exact contract month and opening price it came from.

**The two percentage columns are stored as fractions**, which is what a
spreadsheet's Percent format expects: select columns H and I, then
Format → Number → Percent, and they render as −3.12%, +0.29% and so on.
Storing 3.12 for 3.12% would render as 312%, because Percent format
multiplies by 100.

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

`7,37 15-20 * * 1-5` (UTC) — twice an hour, Monday to Friday.

GitHub's scheduler is best-effort, not a clock. It delays ticks, silently
drops them under load, and throttles frequent schedules hardest. Measured on
this repo, an every-15-minutes schedule delivered **2 of 24 ticks** in a day —
one 3.5 minutes late, one at 23:34 UTC, nearly three hours outside its own
window.

So the design does not depend on any particular tick landing. It asks for
fewer ticks, on off-congestion minutes rather than on the hour, and each run
that does fire **polls internally for 25 minutes** (`--wait-minutes`),
re-checking every 5 minutes until the bulletin goes FINAL. One surviving tick
is enough to capture the day. Runs are idempotent, so extra ticks cost seconds
and change nothing.

A run exits quietly when the bulletin is still PRELIMINARY at the end of its
budget, or when the date is already recorded.

To change the target time, edit `RELEASE_HOUR` / `RELEASE_MIN` in
`run_daily.py` and move the cron window in
`.github/workflows/daily-bulletin.yml` to match.

**If a day is ever missed entirely, it is gone.** The bulletin URL only serves
the current day, so there is no backfill. If you notice a gap, the data has to
come from elsewhere.

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
  average) to mean anything. `history.csv` is accumulating one; once
  there are a few weeks of rows, a volume term can be added.
- **GitHub disables cron on idle repos.** Schedules stop after 60 days without
  repository activity. The job commits `history.csv` on every day it
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
samples/            optional: FINAL bulletins for 25 Aug 2026, used by the tests
history.csv    the file your sheet reads
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
