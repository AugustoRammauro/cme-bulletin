/**
 * Freshness watchdog for the CME bulletin sheet.
 *
 * Why this exists: a broken pipeline cannot tell you it is broken. In October
 * the collector died for ten days and the only signal was GitHub's own
 * failure email, which is easy to miss and says nothing about the data. This
 * runs on Google's clock, reads the sheet, and emails only when the newest
 * trade date has fallen behind. It touches neither GitHub nor CME, so it
 * cannot fail for the same reason the pipeline does, and it needs no
 * credentials of any kind.
 *
 * SETUP (about a minute)
 *   1. In your spreadsheet: Extensions -> Apps Script.
 *   2. Delete whatever is in Code.gs and paste this whole file in. Save.
 *   3. In the toolbar function dropdown pick  installWatchdog  and press Run.
 *      Google will ask for permission to read the sheet and send mail as you;
 *      approve it. That happens once.
 *   4. Done. It now checks every weekday morning.
 *
 * To test it immediately, run  checkFreshness  by hand and read the Execution
 * log. To stop it, run  removeWatchdog.
 */

var TAB_NAME       = 'Bulletin';   // the tab the IMPORTDATA formula fills
var DATE_COLUMN    = 2;            // column B
var ALERT_AFTER    = 2;            // alert once this many business days behind
var CHECK_HOUR     = 14;           // local hour to run (14 = 2pm Montevideo)

/** One-time installer: creates the daily trigger, replacing any previous one. */
function installWatchdog() {
  removeWatchdog();
  ScriptApp.newTrigger('checkFreshness')
    .timeBased()
    .atHour(CHECK_HOUR)
    .everyDays(1)
    .create();
  Logger.log('Watchdog installed: checkFreshness runs daily near %s:00.', CHECK_HOUR);
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
  var d = new Date(from.getTime());
  d.setHours(0, 0, 0, 0);
  var end = new Date(to.getTime());
  end.setHours(0, 0, 0, 0);
  while (d < end) {
    d.setDate(d.getDate() + 1);
    var wd = d.getDay();
    if (wd !== 0 && wd !== 6) n++;
  }
  return n;
}

/**
 * Parse whatever the Date cell holds.
 * IMPORTDATA may hand back a real Date, or the literal string 'DD/MM/YYYY'
 * if the locale did not coerce it. Both are handled; anything else is skipped.
 */
function parseTradeDate(value) {
  if (value instanceof Date && !isNaN(value.getTime())) return value;
  var m = String(value).trim().match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (!m) return null;
  return new Date(Number(m[3]), Number(m[2]) - 1, Number(m[1]));
}

function newestTradeDate() {
  var sheet = SpreadsheetApp.getActive().getSheetByName(TAB_NAME);
  if (!sheet) throw new Error('No tab named "' + TAB_NAME + '"');
  var values = sheet.getRange(1, DATE_COLUMN, sheet.getLastRow(), 1).getValues();
  var newest = null;
  for (var i = 0; i < values.length; i++) {
    var d = parseTradeDate(values[i][0]);
    if (d && (newest === null || d > newest)) newest = d;
  }
  return newest;
}

function checkFreshness() {
  var today = new Date();
  var wd = today.getDay();
  if (wd === 0 || wd === 6) {
    Logger.log('Weekend - skipping.');
    return;
  }

  var newest;
  try {
    newest = newestTradeDate();
  } catch (err) {
    notify('CME sheet: cannot read the data',
           'The watchdog could not read the ' + TAB_NAME + ' tab.\n\n' +
           err.message + '\n\nThe IMPORTDATA formula may be erroring.');
    return;
  }

  if (!newest) {
    notify('CME sheet: no data found',
           'The ' + TAB_NAME + ' tab has no readable dates in column ' +
           DATE_COLUMN + '. The IMPORTDATA formula may be broken or empty.');
    return;
  }

  // The bulletin publishes one business day behind, so a lag of 1 is normal
  // and a lag of 2 is a single dropped run that the collector should self-heal.
  var behind = businessDaysBetween(newest, today);
  var stamp = Utilities.formatDate(newest, Session.getScriptTimeZone(), 'dd/MM/yyyy');
  Logger.log('Newest trade date %s, %s business day(s) behind.', stamp, behind);

  if (behind > ALERT_AFTER) {
    notify('CME data is ' + behind + ' business days stale',
           'The newest trade date in your sheet is ' + stamp + ', which is ' +
           behind + ' business days behind today.\n\n' +
           'One business day of lag is normal. More than ' + ALERT_AFTER +
           ' means the collector has stopped and its own catch-up is not keeping up.\n\n' +
           'Check: https://github.com/AugustoRammauro/cme-bulletin/actions\n\n' +
           'Worth acting quickly: CME only serves about five trade days for ' +
           'free, so a gap left longer than that becomes permanent.');
  }
}

function notify(subject, body) {
  MailApp.sendEmail(Session.getEffectiveUser().getEmail(), '[CME] ' + subject, body);
  Logger.log('Alert sent: %s', subject);
}
