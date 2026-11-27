/* Exportar a Excel y PDF: lo usan Precio vs EPS y Sectores.
   Excel: lee las tablas tal como se ven en la página (los números quedan como números,
   los porcentajes como porcentaje) y arma un .xlsx con una hoja por tabla.
   PDF: imprime la página con un formato de impresión (horizontal, sin botones);
   en el cuadro de impresión elegir "Guardar como PDF". */
(function () {
  "use strict";
  let XL = null;
  function cargar() {
    if (XL) return Promise.resolve(XL);
    return new Promise((ok, mal) => {
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js";
      s.onload = () => { XL = window.XLSX; ok(XL); };
      s.onerror = () => mal(new Error("No se pudo cargar el generador de Excel. Revisá la conexión y probá de nuevo."));
      document.head.appendChild(s);
    });
  }

  // Texto principal de una celda (sin las aclaraciones chicas) y las aclaraciones aparte
  function textos(td) {
    const c = td.cloneNode(true);
    const notas = [...c.querySelectorAll("small, .sub, .tag")].map(x => x.innerText.trim()).filter(Boolean);
    c.querySelectorAll("small, .sub, .tag, button, select, input").forEach(x => x.remove());
    let t = c.innerText.replace(/\s+/g, " ").trim();
    const sel = td.querySelector("select"); if (sel) t = sel.value;
    const inp = td.querySelector("input"); if (inp && !t) t = inp.value;
    return { t, notas };
  }

  // "1.234,56" → 1234.56 · "+12,3%" → 0.123 (con formato %) · "18,2x" → 18.2 · "2,86e" → 2.86 estimado
  function valor(t) {
    let s = t.replace(/^(US\$|\$)\s*/, "").trim();
    let est = false, pct = false, mult = false;
    if (/^[-+−]?[\d.,]+e$/.test(s)) { est = true; s = s.slice(0, -1); }
    if (/%$/.test(s)) { pct = true; s = s.slice(0, -1); }
    if (/x$/.test(s)) { mult = true; s = s.slice(0, -1); }
    s = s.replace("−", "-");
    if (!/^[-+]?\d{1,3}(\.\d{3})*(,\d+)?$|^[-+]?\d+(,\d+)?$/.test(s)) return null;
    const n = parseFloat(s.replace(/\./g, "").replace(",", "."));
    if (!isFinite(n)) return null;
    return { v: pct ? n / 100 : n, z: pct ? "0.0%" : mult ? '0.0"x"' : (Math.abs(n) >= 1000 ? "#,##0" : "#,##0.00"), est };
  }

  // Tabla HTML → filas de celdas de Excel
  function tabla(tb, titulo) {
    const filas = [];
    if (titulo) filas.push([{ v: titulo, t: "s", bold: true }]);
    tb.querySelectorAll("tr").forEach(tr => {
      const fila = [];
      [...tr.children].forEach(td => {
        const { t, notas } = textos(td);
        const span = +td.getAttribute("colspan") || 1;
        const num = td.tagName === "TD" ? valor(t) : null;
        const extra = [td.getAttribute("title") || "", ...notas].filter(Boolean).join(" · ");
        if (num) fila.push({ v: num.v, t: "n", z: num.z, nota: [num.est ? "Estimado (consenso)" : "", extra].filter(Boolean).join(" · ") });
        else fila.push({ v: t, t: "s", nota: td.tagName === "TD" ? extra : "" });
        for (let i = 1; i < span; i++) fila.push({ v: "", t: "s" });
      });
      filas.push(fila);
    });
    return filas;
  }

  async function excel(nombre, hojas) {
    const X = await cargar();
    const wb = X.utils.book_new();
    hojas.forEach(h => {
      const ws = {};
      let maxC = 0;
      h.filas.forEach((fila, r) => {
        maxC = Math.max(maxC, fila.length);
        fila.forEach((c, k) => {
          if (c == null) return;
          const cell = typeof c === "object" ? { v: c.v, t: c.t || (typeof c.v === "number" ? "n" : "s") } : { v: c, t: typeof c === "number" ? "n" : "s" };
          if (c.z) cell.z = c.z;
          if (c.nota) cell.c = [{ a: "Tablero", t: c.nota }];
          ws[X.utils.encode_cell({ r, c: k })] = cell;
        });
      });
      ws["!ref"] = X.utils.encode_range({ s: { r: 0, c: 0 }, e: { r: Math.max(0, h.filas.length - 1), c: Math.max(0, maxC - 1) } });
      ws["!cols"] = Array.from({ length: maxC }, (_, k) => ({ wch: k === 0 ? 30 : 13 }));
      X.utils.book_append_sheet(wb, ws, h.nombre.replace(/[\\/?*[\]:]/g, "").slice(0, 31));
    });
    X.writeFile(wb, nombre);
  }

  function pdf(titulo) {
    const t = document.title;
    if (titulo) document.title = titulo;   // es el nombre que propone el navegador para el PDF
    window.print();
    setTimeout(() => { document.title = t; }, 1000);
  }

  const hoy = () => { const d = new Date(); return d.getFullYear() + String(d.getMonth() + 1).padStart(2, "0") + String(d.getDate()).padStart(2, "0"); };
  window.Exportar = { excel, tabla, pdf, hoy, textos, valor };
})();
