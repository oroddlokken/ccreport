// Server-rendered numbers and stamps are en-US/ISO; only the browser knows the
// reader's locale, so it reformats them in place. The data attribute carries
// the raw value, the element's text is the fallback that stands with
// scripting off or on any value Intl refuses.
(function () {
  const usd = new Intl.NumberFormat(undefined, {
    style: "currency", currency: "USD", currencyDisplay: "narrowSymbol",
  });
  const nok = new Intl.NumberFormat(undefined, {
    style: "currency", currency: "NOK", currencyDisplay: "narrowSymbol", maximumFractionDigits: 0,
  });
  const int = new Intl.NumberFormat();
  const stamp = new Intl.DateTimeFormat(undefined, { dateStyle: "short", timeStyle: "short" });
  const day = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });

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
})();
