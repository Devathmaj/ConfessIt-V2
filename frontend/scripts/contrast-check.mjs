// contrast-check.mjs — run: node scripts/contrast-check.mjs
// Computes WCAG 2.x contrast for token pairs, including alpha compositing.
// Source of truth: ConfessIt_Color_System_Implementation_Guide.md Appendix B.
// Exits non-zero on any failure, so it can gate CI. Update the LIGHT / DARK
// objects whenever tokens change (or generate them from the token files).

const hex = (h) => {
  h = h.replace('#', '');
  if (h.length === 3) h = [...h].map((c) => c + c).join('');
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
};
const lin = (v) => {
  v /= 255;
  return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
};
const lum = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
const ratio = (a, b) => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
// Composite a foreground (hex) at alpha over an opaque background (hex)
const over = (fgHex, alpha, bgHex) => {
  const f = hex(fgHex), b = hex(bgHex);
  return f.map((c, i) => Math.round(alpha * c + (1 - alpha) * b[i]));
};

// ---- Worst-case backgrounds sampled from the page gradients ----
const LIGHT_CANVAS = ['#FFF3FA', '#FFE5F2', '#F9C9DC', '#FEC2E6', '#FFCCE5', '#FAB6D4', '#FBBFE1'];
const DARK_CANVAS  = ['#181014', '#211219', '#1B1016', '#3C132E', '#3F122D'];

const LIGHT = {
  textPrimary: '#4A1733', textSecondary: '#7E4665', textOnCanvas: '#66304F',
  accent: '#AC326E', accentText: '#FFFFFF', focus: '#8E2357', link: '#8E2357',
  surfaceSolid: '#FFF7FB', surfaceAlpha: ['#FFF7FB', 0.90],
};
const DARK = {
  textPrimary: '#FFE7E4', textSecondary: '#FFC1B6', accent: '#FF7A7A',
  accentText: '#2A1122', focus: '#FFC1B6',
  surfaceSolid: '#321B29', surfaceAlpha: ['#321B29', 0.94],
};

const rows = [];
const check = (label, fg, bg, min) => {
  const r = ratio(fg, bg);
  rows.push({ label, fg: hex2(fg), bg: hex2(bg), ratio: r.toFixed(2), min, pass: r >= min ? 'PASS' : 'FAIL' });
};
const hex2 = (c) => (Array.isArray(c) ? '#' + c.map((v) => v.toString(16).padStart(2, '0')).join('').toUpperCase() : c);
const rgb = (c) => (Array.isArray(c) ? c : hex(c));

// Light: text on every canvas stop (text sits directly on the gradient)
for (const bg of LIGHT_CANVAS) {
  check(`L textPrimary on canvas ${bg}`, rgb(LIGHT.textPrimary), hex(bg), 4.5);
  check(`L textOnCanvas on canvas ${bg}`, rgb(LIGHT.textOnCanvas), hex(bg), 4.5);
  check(`L focus on canvas ${bg}`, rgb(LIGHT.focus), hex(bg), 3);
}
// Light: text on translucent card composited over each canvas stop
for (const bg of LIGHT_CANVAS) {
  const card = over(...LIGHT.surfaceAlpha, bg);
  check(`L textPrimary on card@${bg}`, hex(LIGHT.textPrimary), card, 4.5);
  check(`L textSecondary on card@${bg}`, hex(LIGHT.textSecondary), card, 4.5);
  check(`L accent text on card@${bg}`, hex(LIGHT.accent), card, 4.5);
}
check('L button white on accent', hex('#FFFFFF'), hex(LIGHT.accent), 4.5);

// Romantic ramp (light): white fills on the deepened romantic + romantic text
// on the worst canvas glow and on cards (user-reported white-on-white fix).
for (const bg of LIGHT_CANVAS) {
  check(`L romantic text on canvas ${bg}`, hex('#9B173F'), hex(bg), 4.5);
}
check('L white on romantic fill', hex('#FFFFFF'), hex('#9B173F'), 4.5);
check('L romantic-dark on love-pale fill', hex('#93153B'), hex('#FDE3F0'), 4.5);
check('L tile white icon on romantic fill', hex('#FFFFFF'), hex('#9B173F'), 3);
check('L tile plum icon on love-pale fill', hex('#4A1733'), hex('#FDE3F0'), 3);

// Dark
for (const bg of DARK_CANVAS) {
  check(`D textPrimary on canvas ${bg}`, hex(DARK.textPrimary), hex(bg), 4.5);
  check(`D textSecondary on canvas ${bg}`, hex(DARK.textSecondary), hex(bg), 4.5);
  const card = over(...DARK.surfaceAlpha, bg);
  check(`D textPrimary on card@${bg}`, hex(DARK.textPrimary), card, 4.5);
  check(`D textSecondary on card@${bg}`, hex(DARK.textSecondary), card, 4.5);
  check(`D accent (UI/icon) on card@${bg}`, hex(DARK.accent), card, 3);
}
check('D button plum on coral', hex(DARK.accentText), hex(DARK.accent), 4.5);

console.table(rows);
const fails = rows.filter((r) => r.pass === 'FAIL');
if (fails.length) { console.error(`\n${fails.length} failing pair(s)`); process.exit(1); }
console.log('\nAll pairs pass.');
