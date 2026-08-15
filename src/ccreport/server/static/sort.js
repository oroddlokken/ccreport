// Column sorting for the tables the server renders. The rows arrive in the
// order the query chose and this re-orders the ones already on the page, so a
// sort costs no request and a browser with scripting off keeps the server's
// order.

document.addEventListener("DOMContentLoaded", function () {
  // The raw value format.js reads, which is what a column sorts on: the text
  // beside it is locale-formatted, and "1 234,56" compares as a string.
  const NUMERIC = ["data-usd", "data-nok", "data-int", "data-ts"];

  function carrier(cell, attr) {
    return cell.matches("[" + attr + "]") ? cell : cell.querySelector("[" + attr + "]");
  }

  // null for a blank cell, a number where one was carried or parsed, the
  // trimmed text otherwise.
  function value(cell) {
    for (const attr of NUMERIC) {
      const el = carrier(cell, attr);
      if (!el) continue;
      const raw = el.getAttribute(attr);
      return raw === "" ? null : Number(raw);
    }
    const day = carrier(cell, "data-day");
    if (day) {
      const raw = day.getAttribute("data-day");
      return raw === "" ? null : Date.parse(raw + "T00:00:00");
    }
    const text = cell.textContent.trim();
    if (text === "" || text === "—") return null;
    // Digits, separators and a trailing percent: the share column, and nothing
    // that has a data attribute of its own.
    if (/^[-+]?[\d.,\s\u00a0\u202f]+%?$/.test(text)) {
      const bare = Number(text.replace(/[%,\s\u00a0\u202f]/g, ""));
      if (!Number.isNaN(bare)) return bare;
    }
    return text;
  }

  function compare(a, b, numeric) {
    if (numeric) return a - b;
    return String(a).localeCompare(String(b), CCREPORT_LOCALE, { numeric: true, sensitivity: "base" });
  }

  function sortable(rows, index) {
    // An actions column holds a form per row; sorting it would order the rows
    // by nothing the reader can see.
    return rows.every((row) => {
      const cell = row.cells[index];
      return cell && !cell.querySelector("form, button, input");
    });
  }

  function enhance(table) {
    const head = table.tHead && table.tHead.rows[0];
    const body = table.tBodies[0];
    if (!head || !body) return;
    const rows = [...body.rows];
    // One row cannot be re-ordered, and a colspan cell is the empty state
    // rather than a column's value.
    if (rows.length < 2 || rows.some((row) => row.querySelector("[colspan]"))) return;

    [...head.cells].forEach((th, index) => {
      if (!th.textContent.trim() || !sortable(rows, index)) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "sort";
      while (th.firstChild) button.appendChild(th.firstChild);
      th.appendChild(button);
      th.classList.add("sortable");

      button.addEventListener("click", () => {
        const keys = new Map(rows.map((row) => [row, value(row.cells[index])]));
        const filled = [...keys.values()].filter((v) => v !== null);
        const numeric = filled.length > 0 && filled.every((v) => typeof v === "number");
        // A first click on a count answers "which is biggest"; on a name it
        // answers "where is the one I am looking for".
        const first = numeric ? "descending" : "ascending";
        const was = th.getAttribute("aria-sort");
        const now = was === first ? (first === "ascending" ? "descending" : "ascending") : first;
        const factor = now === "ascending" ? 1 : -1;

        rows.sort((x, y) => {
          const a = keys.get(x);
          const b = keys.get(y);
          // Blanks sit at the end whichever way the column points; a row with
          // no value is not the smallest one.
          if (a === null || b === null) return a === b ? 0 : a === null ? 1 : -1;
          return factor * compare(a, b, numeric);
        });
        [...head.cells].forEach((other) => other.removeAttribute("aria-sort"));
        th.setAttribute("aria-sort", now);
        body.append(...rows);
      });
    });
  }

  // uPlot renders its legend as a table and owns those rows.
  for (const table of document.querySelectorAll("table")) {
    if (!table.closest(".u-legend")) enhance(table);
  }
});
