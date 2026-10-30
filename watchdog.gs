/**
 * Freshness watchdog for the CME bulletin pipeline.
 *
 * Why this exists: a broken pipeline cannot tell you it is broken. In October
 * the collector died for ten days and the only signal was GitHub's own
 * failure email, which is easy to miss and says nothing about the data. Six
 * trading days were lost permanently because CME only serves about five days
 * of history for free.
 *
 * What it checks, and why it checks two things:
 *
 *   1. history.csv on GitHub - the authoritative record. This is what tells
 *      you whether the COLLECTOR is alive.
 *   2. the Bulletin tab in this sheet - what you actually see. This tells you
 *      whether the IMPORTDATA formula is keeping up.
 *
 * Checking only the sheet would be wrong: IMPORTDATA refreshes lazily, so a
 * spreadsheet left closed for a week can sit frozen on old values while the
 * collector is working fine. That would produce a daily false alarm during
 * exactly the absences this is meant to cover.
 *
 * The two alerts are deliberately not equally sensitive. A stalled collector
 * loses data permanently, so it alerts after 2 business days. A lagging sheet
 * loses nothing - history.csv still has everything, and opening the sheet
 * refreshes it - so that one stays quiet for 10 business days and only fires
 * if the formula is genuinely dead. A week away with the sheet closed should
 * produce no email at all.
 *
 * It runs on Google's clock and needs no tokens: the repository is public.
 *
 * SETUP
 *   1. Extensions -> Apps Script.
 *   2. Replace everything in the editor with this file. Save.
 *   3. Pick  installWatchdog  in the function dropdown and press Run.
 *      Approve the permissions when asked (reading the sheet, sending mail
 *      as you, and fetching a public URL). Once only.
 *
 * Run  checkFreshness  by hand any time to see the current state in the log.
 * Run  removeWatchdog  to stop it.
 */

var HISTORY_URL = 'https://raw.githubusercontent.com/AugustoRammauro/cme-bulletin/main/history.csv';
var ACTIONS_URL = 'https://github.com/AugustoRammauro/cme-bulletin/actions';

var TAB_NAME    = 'Bulletin';   // the tab the IMPORTDATA formula fills
var DATE_COLUMN = 2;            // column B
var ALERT_AFTER = 2;            // collector: alert past this many business days
var SHEET_LAG   = 10;           // sheet: alert only if it trails GitHub badly
var CHECK_HOUR  = 14;           // local hour to run

function installWatchdog() {
  removeWatchdog();
  ScriptApp.newTrigger('checkFreshness')
    .timeBased().atHour(CHECK_HOUR).everyDays(1).create();
  Logger.log('Watchdog installed: checkFreshness runs daily near %s:00.',
             String(CHECK_HOUR));
  checkFreshness();
}

function removeWatchdog() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'checkFreshness') ScriptApp.deleteTrigger(t);
  });
}

/** Business days strictly between two dates, ignoring holidays. */
function businessDaysBetween(from, to) {
  var n = 0;
  var d = new Date(from.getTime()); d.setHours(0, 0, 0, 0);
  var end = new Date(to.getTime()); end.setHours(0, 0, 0, 0);
  while (d < end) {
    d.setDate(d.getDate() + 1);
    var wd = d.getDay();
    if (wd !== 0 && wd !== 6) n++;
  }
  return n;
}

/**
 * Parse a trade date.
 * The CSV always holds 'DD/MM/YYYY'. The sheet may hand back a real Date if
 * the locale coerced it, or that same string if it did not.
 */
function parseTradeDate(value) {
  if (value instanceof Date && !isNaN(value.getTime())) return value;
  var m = String(value).trim().match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (!m) return null;
  return new Date(Number(m[3]), Number(m[2]) - 1, Number(m[1]));
}

function newest(dates) {
  var best = null;
  for (var i = 0; i < dates.length; i++) {
    var d = parseTradeDate(dates[i]);
    if (d && (best === null || d > best)) best = d;
  }
  return best;
}

/** Newest trade date in history.csv on GitHub - the authoritative answer. */
function newestOnGitHub() {
  var url = HISTORY_URL + '?t=' + Date.now();     // defeat the CDN cache
  var res = UrlFetchApp.fetch(url, {muteHttpExceptions: true});
  if (res.getResponseCode() !== 200) {
    throw new Error('GitHub returned ' + res.getResponseCode());
  }
  var lines = res.getContentText().split('\n');
  var dates = [];
  for (var i = 1; i < lines.length; i++) {            // skip the header
    var cols = lines[i].split(',');
    if (cols.length > 1) dates.push(cols[1]);
  }
  return newest(dates);
}

/** Newest trade date the spreadsheet is currently showing. */
function newestInSheet() {
  var sheet = SpreadsheetApp.getActive().getSheetByName(TAB_NAME);
  if (!sheet) throw new Error('No tab named "' + TAB_NAME + '"');
  var col = sheet.getRange(1, DATE_COLUMN, sheet.getLastRow(), 1).getValues();
  var flat = [];
  for (var i = 0; i < col.length; i++) flat.push(col[i][0]);
  return newest(flat);
}

function fmt(d) {
  return Utilities.formatDate(d, Session.getScriptTimeZone(), 'dd/MM/yyyy');
}

function checkFreshness() {
  var today = new Date();
  if (today.getDay() === 0 || today.getDay() === 6) {
    Logger.log('Weekend - skipping.');
    return;
  }

  // 1. Is the collector alive? This is the question that matters.
  var git = null;
  try {
    git = newestOnGitHub();
  } catch (err) {
    notify('cannot reach the data file',
           'The watchdog could not read history.csv from GitHub.\n\n' +
           err.message + '\n\n' + ACTIONS_URL);
    return;
  }

  if (!git) {
    notify('history.csv looks empty',
           'history.csv was fetched but contained no readable dates.\n\n' +
           ACTIONS_URL);
    return;
  }

  var behind = businessDaysBetween(git, today);
  Logger.log('GitHub newest %s, %s business day(s) behind.',
             fmt(git), String(behind));

  if (behind > ALERT_AFTER) {
    notify('data is ' + behind + ' business days stale',
           'The newest trade date in history.csv is ' + fmt(git) + ', ' +
           behind + ' business days behind today.\n\n' +
           'One business day of lag is normal. More than ' + ALERT_AFTER +
           ' means the collector has stopped and its own catch-up is not ' +
           'keeping up.\n\n' + ACTIONS_URL + '\n\n' +
           'Worth acting quickly: CME only serves about five trade days for ' +
           'free, so a gap left longer than that becomes permanent.');
    return;                       // the collector is the problem; say so once
  }

  // 2. The collector is fine. Is the sheet actually showing its output?
  var sheet = null;
  try {
    sheet = newestInSheet();
  } catch (err) {
    notify('cannot read the ' + TAB_NAME + ' tab',
           err.message + '\n\nThe IMPORTDATA formula may be erroring.');
    return;
  }

  if (!sheet) {
    notify('the ' + TAB_NAME + ' tab is empty',
           'history.csv is current (' + fmt(git) + ') but the sheet shows no ' +
           'dates. The IMPORTDATA formula is probably broken.');
    return;
  }

  var lag = businessDaysBetween(sheet, git);
  Logger.log('Sheet newest %s, %s business day(s) behind GitHub.',
             fmt(sheet), String(lag));

  if (lag > SHEET_LAG) {
    notify('your sheet is behind the data',
           'The collector is healthy - history.csv is current to ' + fmt(git) +
           '.\n\nBut the ' + TAB_NAME + ' tab is only showing up to ' +
           fmt(sheet) + '.\n\nIMPORTDATA refreshes lazily, so this often ' +
           'clears itself when you next open the sheet. If it does not, ' +
           'click A1, delete the formula, press Enter, then Ctrl+Z.');
  }
}

function notify(subject, body) {
  MailApp.sendEmail(Session.getEffectiveUser().getEmail(), '[CME] ' + subject, body);
  Logger.log('Alert sent: %s', subject);
}
