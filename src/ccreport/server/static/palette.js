// The categorical hues every chart on this server draws from, read off the
// --chart-N tokens in app.css so the account bars (CSS) and the uPlot series
// (JS) cannot drift apart. Fixed order: a series keeps its colour when a
// filter drops the ones beside it. The validation story lives with the tokens.
(function () {
  const styles = getComputedStyle(document.documentElement);
  window.CCREPORT_COLORS = [1, 2, 3, 4, 5, 6]
    .map((i) => styles.getPropertyValue("--chart-" + i).trim())
    .filter(Boolean);
})();
