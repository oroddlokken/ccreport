// One entity's charts. Each spec from the server carries its own axis, its
// traces and the unit they are in -- four plots rather than one with a toggle,
// because dollars, tokens and calls do not share a scale.

(function () {
  const specs = JSON.parse(document.getElementById("chart-data").textContent);
  const colors = window.CCREPORT_COLORS;
  const INK = "#8b93a7";
  const GRID = "#272b36";

  function stamp(value) {
    // A date alone is midnight local; an hour stamp already says which hour.
    return Date.parse(value.length > 10 ? value : value + "T00:00:00") / 1000;
  }

  function tick(unit) {
    if (unit === "usd") return (u, values) => values.map((v) => "$" + v.toFixed(2));
    if (unit === "tokens") {
      return (u, values) => values.map((v) => (v >= 1000 ? (v / 1000).toFixed(0) + "K" : v));
    }
    return (u, values) => values.map((v) => v);
  }

  function draw(spec, box) {
    const xs = spec.axis.map(stamp);
    const bars = spec.traces.length === 1;
    const opts = {
      width: box.clientWidth || 480,
      height: 200,
      // Live, so moving across the plot reads the values out under the title
      // instead of asking the eye to measure against the axis.
      legend: { live: true },
      cursor: { x: true, y: false },
      series: [
        {},
        ...spec.traces.map((trace, i) => ({
          label: trace.label,
          stroke: colors[i % colors.length],
          fill: colors[i % colors.length] + (bars ? "cc" : "22"),
          width: 2,
          paths: bars ? uPlot.paths.bars({ size: [0.7, 40], radius: 0.2 }) : undefined,
          points: { show: !bars, size: 8 },
        })),
      ],
      axes: [
        { stroke: INK, grid: { stroke: GRID } },
        { stroke: INK, grid: { stroke: GRID }, values: tick(spec.unit) },
      ],
    };
    return new uPlot(opts, [xs, ...spec.traces.map((t) => t.values)], box);
  }

  const drawn = [];
  specs.forEach((spec) => {
    const box = document.querySelector(`[data-chart="${spec.key}"] .plot`);
    if (!box || !spec.axis.length) return;
    drawn.push([spec, box, draw(spec, box)]);
  });

  window.addEventListener("resize", () => {
    drawn.forEach((entry) => {
      entry[2].setSize({ width: entry[1].clientWidth || 480, height: 200 });
    });
  });
})();
