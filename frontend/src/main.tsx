import { createRoot } from 'react-dom/client'
import App from './App.tsx'
import './index.css'
import { ThemeProvider } from '@/components/theme-provider'
import { syncServerClock } from '@/lib/serverClock'
import { initFloralCaching } from '@/lib/floralCache'

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
