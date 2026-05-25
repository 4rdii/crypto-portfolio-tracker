// Shared helpers

// HTML escape for values we interpolate into innerHTML templates. Anything
// that came from the user (labels, notes, symbol names from scam tokens,
// addresses) MUST go through esc() before being dropped into a template
// literal — otherwise a `<img src=x onerror=...>` in a wallet label pops a
// script in anyone who views that account (self-XSS today, shared-view
// XSS the moment we add an admin panel or a shared portfolio link).
function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtUSD(n) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  const sign = n < 0 ? '-' : '';
  const a = Math.abs(n);
  if (a >= 1e6) return sign + '$' + (a/1e6).toFixed(2) + 'M';
  if (a >= 1e3) return sign + '$' + a.toLocaleString('en-US', { maximumFractionDigits: 0 });
  if (a >= 1)   return sign + '$' + a.toLocaleString('en-US', { maximumFractionDigits: 2 });
  if (a >= 0.01) return sign + '$' + a.toFixed(4);
  return sign + '$' + a.toFixed(6);
}

function fmtNum(n) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  const a = Math.abs(n);
  if (a >= 1e6) return (n/1e6).toFixed(2) + 'M';
  if (a >= 1e3) return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
  if (a >= 1)   return n.toLocaleString('en-US', { maximumFractionDigits: 4 });
  return n.toFixed(6);
}

function pct(n) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  const sign = n >= 0 ? '+' : '';
  return sign + n.toFixed(2) + '%';
}

function chartOpts(showY = false) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: '#8b92a5', font: { size: 10 } }, grid: { display: false } },
      y: {
        display: showY,
        ticks: { color: '#8b92a5', font: { size: 10 }, callback: v => '$' + v.toLocaleString() },
        grid: { color: '#1c2029', drawBorder: false }
      }
    }
  };
}

async function scanAll() {
  const btn = event.target;
  const oldText = btn.textContent;
  btn.textContent = '↻ Scanning...';
  btn.disabled = true;
  try {
    const r = await fetch('/api/scan-all', { method: 'POST' }).then(r => r.json());
    btn.textContent = `✓ ${r.scanned.length} scanned`;
    setTimeout(() => location.reload(), 800);
  } catch (e) {
    btn.textContent = '✗ Failed';
    setTimeout(() => btn.textContent = oldText, 2000);
  }
}
