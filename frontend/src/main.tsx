import { createRoot } from 'react-dom/client'
import App from './App.tsx'
import './index.css'
import './edge-style.css'
import { ThemeProvider } from '@/components/theme-provider'
import { syncServerClock } from '@/lib/serverClock'
import { initFloralCaching } from '@/lib/floralCache'

// Romantic edge restyle: one-line switch between style directions.
// Default preset is "leaf". Override via localStorage "edge-style"
// (e.g. localStorage.setItem("edge-style","petal")) or VITE_EDGE_PRESET.
// "default" or "none" restores the pre-restyle shapes.
try {
  const stored = localStorage.getItem('edge-style');
  const raw = stored ?? import.meta.env.VITE_EDGE_PRESET ?? 'leaf';
  const preset = typeof raw === 'string' ? raw.trim().toLowerCase() : '';
  if (preset && preset !== 'default' && preset !== 'none') {
    document.documentElement.dataset.edge = preset;
  } else {
    delete document.documentElement.dataset.edge;
  }
} catch {
  const fallback = import.meta.env.VITE_EDGE_PRESET ?? 'leaf';
  if (typeof fallback === 'string' && fallback.trim()) {
    const normalized = fallback.trim().toLowerCase();
    if (normalized !== 'default' && normalized !== 'none') {
      document.documentElement.dataset.edge = normalized;
    }
  } else {
    document.documentElement.dataset.edge = 'leaf';
  }
}

// Ask the server what time it is before anything renders a countdown or an expiry badge.
// Deliberately not awaited: until it lands the offset is zero, so the first paint uses the
// device clock exactly as before and then corrects itself on the next render that depends
// on time. Blocking the bundle on a network call would be a worse trade for ~5ms of skew.
void syncServerClock()

// Floral branches are fetched idle and cached heavily (service worker +
// Cache Storage), starting before anyone logs in. Deferred internally, so
// the login video, poster and first paint always win the race.
initFloralCaching()

createRoot(document.getElementById("root")!).render(
    <ThemeProvider 
        attribute="class" 
        defaultTheme="dark" 
        storageKey="vite-ui-theme"
    >
        <App />
    </ThemeProvider>
);
