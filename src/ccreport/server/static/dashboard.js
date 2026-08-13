// The dashboard's two toggles and the chart they drive: which series uPlot
// draws, and which breakdown table is visible. The page itself is
// server-rendered.

(function () {
  const payload = JSON.parse(document.getElementById("chart-data").textContent);
  const box = document.getElementById("chart");
  if (!box || !payload.days.length) return;

  // Legible on the dark background, and apart from each other. More accounts
  // than colours cycles.
  const COLORS = ["#7aa2f7", "#7bd88f", "#e0af68", "#bb9af7", "#f7768e", "#2ac3de"];

  const xs = payload.days.map((day) => Date.parse(day + "T00:00:00") / 1000);
  let metric = "cost";
  let chart = null;

  function draw() {
    const data = [xs, ...payload.series.map((s) => s[metric])];
    const opts = {
      width: box.clientWidth || 640,
      height: 260,
      series: [
        {},
        ...payload.series.map((s, i) => ({
          label: s.account,
          stroke: COLORS[i % COLORS.length],
          fill: COLORS[i % COLORS.length] + "22",
          width: 2,
        })),
      ],
      axes: [
        { stroke: "#8b93a7", grid: { stroke: "#272b36" } },
        { stroke: "#8b93a7", grid: { stroke: "#272b36" } },
      ],
      legend: { live: false },
    };
    if (chart) chart.destroy();
    chart = new uPlot(opts, data, box);
  }

  document.getElementById("metric-cost").addEventListener("click", (e) => {
    e.preventDefault();
    metric = "cost";
    document.getElementById("metric-cost").classList.add("on");
    document.getElementById("metric-tokens").classList.remove("on");
    draw();
  });
  document.getElementById("metric-tokens").addEventListener("click", (e) => {
    e.preventDefault();
    metric = "tokens";
    document.getElementById("metric-tokens").classList.add("on");
    document.getElementById("metric-cost").classList.remove("on");
    draw();
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
    });
  });

  window.addEventListener("resize", draw);
  draw();
})();
