// Server-rendered numbers and stamps are en-US/ISO; this reformats them in
// place. The data attribute carries the raw value, the element's text is the
// fallback that stands with scripting off or on any value Intl refuses.
//
// undefined = the browser's own locale. Loaded in head so the chart scripts
// can share the constant; the rewrite itself waits for the parsed document.
const CCREPORT_LOCALE = undefined;

document.addEventListener("DOMContentLoaded", function () {
  // Not narrowSymbol: a locale that puts the symbol last renders USD as a
  // trailing bare "$", which names no country. "symbol" is "$337.63" in en-US
  // and "337,63 USD" in nb-NO.
  const usd = new Intl.NumberFormat(CCREPORT_LOCALE, {
    style: "currency", currency: "USD", currencyDisplay: "symbol",
  });
  const nok = new Intl.NumberFormat(CCREPORT_LOCALE, {
    style: "currency", currency: "NOK", currencyDisplay: "narrowSymbol", maximumFractionDigits: 0,
  });
  const int = new Intl.NumberFormat(CCREPORT_LOCALE);
  const stamp = new Intl.DateTimeFormat(CCREPORT_LOCALE, { dateStyle: "short", timeStyle: "short" });
  const day = new Intl.DateTimeFormat(CCREPORT_LOCALE, { dateStyle: "medium" });

  const rules = [
    ["data-usd", (v) => usd.format(Number(v))],
    ["data-nok", (v) => nok.format(Number(v))],
    ["data-int", (v) => int.format(Number(v))],
    ["data-ts", (v) => stamp.format(new Date(Number(v) * 1000))],
    ["data-day", (v) => day.format(new Date(v + "T00:00:00"))],
  ];
  for (const [attr, format] of rules) {
    for (const el of document.querySelectorAll("[" + attr + "]")) {
      const raw = el.getAttribute(attr);
      if (raw === "") continue;
      try {
        el.textContent = format(raw);
      } catch {
        // The server's text stays.
      }
    }
  }
});
