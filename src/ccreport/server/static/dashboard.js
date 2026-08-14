// The dashboard's two toggles and the chart they drive: which series uPlot
// draws, and which breakdown table is visible. The page itself is
// server-rendered.

(function () {
  const payload = JSON.parse(document.getElementById("chart-data").textContent);
  const box = document.getElementById("chart");
  if (!box || !payload.days.length) return;

  const COLORS = window.CCREPORT_COLORS;

  const xs = payload.days.map((day) => Date.parse(day + "T00:00:00") / 1000);
  const selected = document.querySelector(".toggle.metric.on");
  let metric = selected ? selected.dataset.metric : "cost";
  let chart = null;

  // Both toggles are real links, so the page works with scripting off. With it
  // on, the click stays local and only rewrites the address bar — which is what
  // a reload reads to open on the tab and series last clicked. The URL is built
  // from the current one rather than the link's href: the other toggle's hrefs
  // still carry what the page was rendered with, and would undo this one.
  function remember(key, value) {
    const url = new URL(window.location.href);
    url.searchParams.set(key, value);
    history.replaceState(null, "", url.pathname + "?" + url.searchParams.toString());
  }

  function draw() {
    const data = [xs, ...payload.series.map((s) => s[metric])];
    const opts = {
      width: box.clientWidth || 640,
      height: 200,
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
        })),
      ],
      scales: {
        // Anchored at zero, with headroom so the spline's overshoot at a peak
        // is drawn rather than clipped flat against the plot edge.
        y: { range: (u, min, max) => [0, max * 1.08 || 1] },
      },
      axes: [
        { stroke: "#8b93a7", grid: { stroke: "#272b36" } },
        { stroke: "#8b93a7", grid: { stroke: "#272b36" } },
      ],
      legend: { live: false },
    };
    if (chart) chart.destroy();
    chart = new uPlot(opts, data, box);
  }

  document.querySelectorAll(".toggle.metric").forEach((tab) => {
    tab.addEventListener("click", (e) => {
      e.preventDefault();
      metric = tab.dataset.metric;
      document.querySelectorAll(".toggle.metric").forEach((t) => {
        t.classList.toggle("on", t === tab);
      });
      remember("metric", metric);
      draw();
    });
  });

  document.querySelectorAll(".toggle.dimension").forEach((tab) => {
    tab.addEventListener("click", (e) => {
      e.preventDefault();
      const wanted = tab.dataset.dimension;
      document.querySelectorAll(".toggle.dimension").forEach((t) => {
        t.classList.toggle("on", t === tab);
      });
      document.querySelectorAll("table.breakdown").forEach((table) => {
        table.hidden = table.dataset.dimension !== wanted;
      });
      remember("by", wanted);
    });
  });

  window.addEventListener("resize", draw);
  draw();
})();
