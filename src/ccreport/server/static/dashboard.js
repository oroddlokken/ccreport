// The dashboard's two toggles and the chart they drive: which series uPlot
// draws, and which breakdown table is visible. The page itself is
// server-rendered.

(function () {
  const payload = JSON.parse(document.getElementById("chart-data").textContent);
  const box = document.getElementById("chart");
  if (!box) return;
  if (!payload.days.length) {
    // A silent hole under the Daily heading reads as broken; say why it is empty.
    box.classList.add("dim");
    box.textContent = "Nothing in this range.";
    return;
  }

  const styles = getComputedStyle(document.documentElement);
  const COLORS = window.CCREPORT_COLORS;
  const INK = styles.getPropertyValue("--dim").trim();
  const GRID = styles.getPropertyValue("--line").trim();

  const xs = payload.days.map((day) => Date.parse(day + "T00:00:00") / 1000);
  const selected = document.querySelector(".toggle.metric.on");
  let metric = selected ? selected.dataset.metric : "cost";
  let chart = null;

  // Short ticks keep the y axis inside its gutter: "$1.9k" fits where
  // "$1,900.00" clips against the plot.
  function compact(value) {
    if (value >= 1e9) return (value / 1e9).toFixed(value >= 1e10 ? 0 : 1) + "B";
    if (value >= 1e6) return (value / 1e6).toFixed(value >= 1e7 ? 0 : 1) + "M";
    if (value >= 1e3) return (value / 1e3).toFixed(value >= 1e4 ? 0 : 1) + "k";
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }

  // Both toggles are real links, so the page works with scripting off. With it
  // on, the click stays local and only rewrites the address bar — which is what
  // a reload reads to open on the tab and series last clicked. The URL is built
  // from the current one rather than the link's href: the other toggle's hrefs
  // still carry what the page was rendered with, and would undo this one.
  function remember(key, value) {
    const url = new URL(window.location.href);
    url.searchParams.set(key, value);
    history.replaceState(null, "", url.pathname + "?" + url.searchParams.toString());
    // The sibling links (ranges, Refresh, the other toggle group) were also
    // rendered with the old value; rewrite their hrefs so a later click
    // carries this choice instead of reverting it.
    const owner = key === "metric" ? "metric" : "dimension";
    document.querySelectorAll("a.toggle").forEach((link) => {
      if (link.classList.contains(owner)) return;
      const target = new URL(link.href);
      target.searchParams.set(key, value);
      link.href = target.pathname + "?" + target.searchParams.toString();
    });
  }

  function mark(tabs, active) {
    tabs.forEach((tab) => {
      tab.classList.toggle("on", tab === active);
      if (tab === active) {
        tab.setAttribute("aria-current", "true");
      } else {
        tab.removeAttribute("aria-current");
      }
    });
  }

  function draw() {
    const data = [xs, ...payload.series.map((s) => s[metric])];
    // CCREPORT_LOCALE comes from format.js; the legend follows the same rules
    // as every other number on the page.
    const usd = new Intl.NumberFormat(CCREPORT_LOCALE, {
      style: "currency", currency: "USD", currencyDisplay: "narrowSymbol",
    });
    const money = (u, v) => (v == null ? "--" : usd.format(v));
    const count = (u, v) => (v == null ? "--" : new Intl.NumberFormat(CCREPORT_LOCALE).format(v));
    const opts = {
      width: box.clientWidth || 640,
      height: 200,
      // Live, so the crosshair and hover points read the day's values out in
      // the legend -- without it they point at nothing.
      legend: { live: true },
      series: [
        {},
        ...payload.series.map((s, i) => ({
          label: s.account,
          stroke: COLORS[i % COLORS.length],
          fill: COLORS[i % COLORS.length] + "22",
          width: 2,
          // Thirty daily points is too dense for markers; the cursor's own
          // hover point still lands on the day under the pointer.
          points: { show: false },
          paths: uPlot.paths.spline(),
          value: metric === "cost" ? money : count,
        })),
      ],
      scales: {
        // Anchored at zero, with headroom so the spline's overshoot at a peak
        // is drawn rather than clipped flat against the plot edge.
        y: { range: (u, min, max) => [0, max * 1.08 || 1] },
      },
      axes: [
        { stroke: INK, grid: { stroke: GRID } },
        {
          stroke: INK,
          grid: { stroke: GRID },
          size: 60,
          values: (u, ticks) => ticks.map((v) => (metric === "cost" ? "$" : "") + compact(v)),
        },
      ],
    };
    if (chart) chart.destroy();
    chart = new uPlot(opts, data, box);
  }

  const metricTabs = [...document.querySelectorAll(".toggle.metric")];
  metricTabs.forEach((tab) => {
    tab.addEventListener("click", (e) => {
      e.preventDefault();
      metric = tab.dataset.metric;
      mark(metricTabs, tab);
      remember("metric", metric);
      draw();
    });
  });

  const dimensionTabs = [...document.querySelectorAll(".toggle.dimension")];
  dimensionTabs.forEach((tab) => {
    tab.addEventListener("click", (e) => {
      e.preventDefault();
      const wanted = tab.dataset.dimension;
      mark(dimensionTabs, tab);
      document.querySelectorAll(".breakdown-clip").forEach((wrap) => {
        wrap.hidden = wrap.dataset.dimension !== wanted;
      });
      remember("by", wanted);
    });
  });

  // A resize only needs a new width, not a rebuild, and one per frame is
  // plenty while a window edge is being dragged.
  let raf = 0;
  window.addEventListener("resize", () => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      if (chart) chart.setSize({ width: box.clientWidth || 640, height: 200 });
    });
  });
  draw();
})();
