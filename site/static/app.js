// BoardWatch client: filters + placeholder signup + price history sparkline
(function(){
  const $ = s => document.querySelector(s);
  // stock table filters
  const t = $('#stock-table');
  if (t) {
    const f = {inst: $('#f-instock'), fam: $('#f-family'), ven: $('#f-vendor'), price: $('#f-price'), out: $('#f-price-out'), used: $('#f-used')};
    const params = new URLSearchParams(location.search);
    if (params.get('family')) f.fam.value = params.get('family');
    if (params.get('instock')) f.inst.checked = true;
    const apply = () => {
      const max = +f.price.value, anyPrice = max >= +f.price.max;
      f.out.textContent = anyPrice ? 'any' : '$' + max;
      let shown = 0;
      t.querySelectorAll('tbody tr').forEach(tr => {
        const d = tr.dataset, usd = d.usd === '' ? null : +d.usd;
        let ok = true;
        if (f.inst.checked && d.instock !== '1') ok = false;
        if (f.fam.value && d.family !== f.fam.value) ok = false;
        if (f.ven.value && d.vendor !== f.ven.value) ok = false;
        if (!f.used.checked && d.used === '1') ok = false;
        if (!anyPrice && (usd === null || usd > max)) ok = false;
        tr.hidden = !ok; if (ok) shown++;
      });
      $('#empty-msg').hidden = shown > 0;
    };
    Object.values(f).forEach(el => el && el.addEventListener('input', apply));
    apply();
  }
  // compare filters
  const c = $('#compare');
  if (c) {
    const fam = $('#c-family'), inst = $('#c-instock');
    const apply = () => c.querySelectorAll('tbody tr').forEach(tr => {
      tr.hidden = (fam.value && tr.dataset.family !== fam.value) || (inst.checked && tr.dataset.instock !== '1');
    });
    fam.addEventListener('input', apply); inst.addEventListener('input', apply);
  }
  // placeholder newsletter
  document.querySelectorAll('form.signup[data-placeholder]').forEach(form => form.addEventListener('submit', e => {
    e.preventDefault();
    const msg = form.parentElement.querySelector('.form-msg');
    msg.textContent = "Thanks! Signups aren't connected yet, so nothing was sent or stored. Check back soon.";
  }));
  // price history
  const h = $('#history');
  if (h) {
    const series = JSON.parse(h.dataset.points || '[]').filter(s => s.points.length);
    if (!series.length) { h.innerHTML = '<p class="dim">No price data yet.</p>'; return; }
    const W = 640, H = 200, P = 34, cols = ['#7fdc6a','#f2b45a','#5fc4b8','#ef5b4c','#c9a0ff'];
    const all = series.flatMap(s => s.points);
    const ts = all.map(p => Date.parse(p.t)), ps = all.map(p => p.price);
    let t0 = Math.min(...ts), t1 = Math.max(...ts); if (t1 === t0) { t0 -= 864e5; t1 += 864e5; }
    let p0 = Math.min(...ps), p1 = Math.max(...ps); const pad = (p1 - p0) * .15 || p1 * .1 || 1; p0 -= pad; p1 += pad;
    const x = t => P + (t - t0) / (t1 - t0) * (W - 2 * P), y = p => H - P + 8 - (p - p0) / (p1 - p0) * (H - 2 * P);
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Price history">`;
    for (let i = 0; i <= 4; i++) { const yy = P - 8 + i * (H - 2 * P) / 4; svg += `<line x1="${P}" x2="${W-P}" y1="${yy}" y2="${yy}" stroke="#2c3a2b" stroke-width="1"/>`; }
    const legend = [];
    series.forEach((s, i) => {
      const col = cols[i % cols.length], sym = {USD:'$',GBP:'£',EUR:'€'}[s.currency] || '';
      const pts = s.points.map(p => [x(Date.parse(p.t)), y(p.price), p]);
      svg += `<polyline fill="none" stroke="${col}" stroke-width="2" points="${pts.map(q => q[0].toFixed(1)+','+q[1].toFixed(1)).join(' ')}"/>`;
      pts.forEach(q => svg += `<circle cx="${q[0].toFixed(1)}" cy="${q[1].toFixed(1)}" r="4" fill="${q[2].in_stock ? col : '#15130d'}" stroke="${col}" stroke-width="2"><title>${s.vendor}: ${sym}${q[2].price} · ${q[2].in_stock ? 'in stock' : 'sold out'} · ${q[2].t}</title></circle>`);
      const last = s.points[s.points.length - 1];
      legend.push(`<span style="color:${col}">● ${s.vendor} ${sym}${last.price.toFixed(2)}</span>`);
    });
    svg += `<text x="${P}" y="${H-6}" fill="#a99f88" font-size="11" font-family="monospace">${new Date(t0).toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"})}</text><text x="${W-P}" y="${H-6}" fill="#a99f88" font-size="11" font-family="monospace" text-anchor="end">${new Date(t1).toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"})}</text></svg>`;
    h.innerHTML = svg + `<p class="small mono" style="display:flex;flex-wrap:wrap;gap:14px">${legend.join('')}</p><p class="small dim">Filled dot = in stock, hollow = sold out.</p>`;
  }
})();
