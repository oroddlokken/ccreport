// One entity's charts. Each spec from the server carries its own axis, its
// traces and the unit they are in -- four plots rather than one with a toggle,
// because dollars, tokens and calls do not share a scale.

(function () {
  const specs = JSON.parse(document.getElementById("chart-data").textContent);
  const styles = getComputedStyle(document.documentElement);
  const colors = window.CCREPORT_COLORS;
  const INK = styles.getPropertyValue("--dim").trim();
  const GRID = styles.getPropertyValue("--line").trim();

  function stamp(value) {
    // A date alone is midnight local; an hour stamp already says which hour.
    return Date.parse(value.length > 10 ? value : value + "T00:00:00") / 1000;
  }

  // Short ticks keep the y axis inside its gutter: "$1.9k" fits where
  // "$1,900.00" clips against the plot.
  function compact(value) {
    if (value >= 1e9) return (value / 1e9).toFixed(value >= 1e10 ? 0 : 1) + "B";
    if (value >= 1e6) return (value / 1e6).toFixed(value >= 1e7 ? 0 : 1) + "M";
    if (value >= 1e3) return (value / 1e3).toFixed(value >= 1e4 ? 0 : 1) + "k";
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }

  // Daily points carry no clock; an hourly axis gets date and 24h time in the
  // reader's locale instead of uPlot's "12:00am" default.
  const dayFmt = new Intl.DateTimeFormat(CCREPORT_LOCALE, { dateStyle: "medium" });
  const hourFmt = new Intl.DateTimeFormat(CCREPORT_LOCALE, { dateStyle: "short", timeStyle: "short" });

  function tick(unit) {
    if (unit === "usd") return (u, values) => values.map((v) => "$" + compact(v));
    if (unit === "tokens") return (u, values) => values.map((v) => compact(v));
    return (u, values) => values.map((v) => v);
  }

  function draw(spec, box) {
    const xs = spec.axis.map(stamp);
    const bars = spec.traces.length === 1;
    const when = spec.axis.some((v) => v.length > 10) ? hourFmt : dayFmt;
    const opts = {
      width: box.clientWidth || 480,
      height: 200,
      // Live, so moving across the plot reads the values out under the title
      // instead of asking the eye to measure against the axis.
      legend: { live: true },
      // One sync key for the page: the charts share a time axis, so hovering
      // a day in one reads the same day out in all of them.
      cursor: { x: true, y: false, sync: { key: "ccreport-detail" } },
      series: [
        { value: (u, ts) => (ts == null ? "--" : when.format(new Date(ts * 1000))) },
        ...spec.traces.map((trace, i) => ({
          label: trace.label,
          stroke: colors[i % colors.length],
          fill: colors[i % colors.length] + (bars ? "cc" : "22"),
          width: 2,
          paths: bars
            ? uPlot.paths.bars({ size: [0.7, 40], radius: 0.05 })
            : uPlot.paths.spline(),
          points: { show: false },
        })),
      ],
      axes: [
        { stroke: INK, grid: { stroke: GRID } },
        { stroke: INK, grid: { stroke: GRID }, size: 60, values: tick(spec.unit) },
      ],
    };
    return new uPlot(opts, [xs, ...spec.traces.map((t) => t.values)], box);
  }

  const drawn = [];
  specs.forEach((spec) => {
    const box = document.querySelector(`[data-chart="${spec.key}"] .plot`);
    if (!box) return;
    if (!spec.axis.length) {
      // An empty bordered panel under a title reads as broken; say why.
      box.classList.add("dim");
      box.textContent = "Nothing in this range.";
      return;
    }
    drawn.push([spec, box, draw(spec, box)]);
  });

  // One resize per frame, reads before writes, so four charts do not
  // interleave layout reads and writes while a window edge is dragged.
  let raf = 0;
  window.addEventListener("resize", () => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      const widths = drawn.map((entry) => entry[1].clientWidth || 480);
      drawn.forEach((entry, i) => entry[2].setSize({ width: widths[i], height: 200 }));
    });
  });
})();
