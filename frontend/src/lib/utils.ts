import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * A profile-picture value the API handed over, reduced to something an `<img>` may load.
 *
 * The API derives these URLs from stored Cloudinary identities, so a well-formed one is
 * always ours -- but `<img src>` is a script-capable sink for SVG, and `data:` URLs in
 * particular execute (`data:image/svg+xml,<svg onload=...>` runs on load). So this is a
 * strict allowlist, not a scheme check:
 *
 * * `https://*.cloudinary.com` -- the delivery host every legitimate picture uses;
 * * `data:image/(png|jpeg|gif|webp);base64,` -- raster inline images only, never SVG or
 *   anything the browser could sniff into script (note the mandatory `;base64`);
 * * site-relative `/...` paths -- same-origin static art like the placeholder itself.
 *
 * Anything else -- `http:` (cleartext), `javascript:`, `data:` SVG/HTML, remote hosts --
 * falls back to the placeholder. A broken or hostile value therefore degrades to a grey
 * tile, never to code.
 */
const CLOUDINARY_DELIVERY_RE = /^https:\/\/[a-z0-9-]+\.cloudinary\.com\//i;
const SAFE_INLINE_IMAGE_RE = /^data:image\/(png|jpeg|jpg|gif|webp);base64,/i;

export function resolveProfilePictureUrl(value?: string | null) {
  if (!value) {
    return '/placeholder.svg'
  }

  if (CLOUDINARY_DELIVERY_RE.test(value)) {
    return value
  }

  if (SAFE_INLINE_IMAGE_RE.test(value)) {
    return value
  }

  if (value.startsWith('/')) {
    return value
  }

  return '/placeholder.svg'
}

export function parseTimestamp(value?: string | null): Date | null {
  if (!value) return null;
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`;
  const milliseconds = Date.parse(normalized);
  return Number.isFinite(milliseconds) ? new Date(milliseconds) : null;
}

/**
 * A timestamp as epoch milliseconds, for ordering and arithmetic.
 *
 * Every API timestamp must reach the browser through `parseTimestamp` rather than a bare
 * `new Date(value)`. The two agree on an offset-carrying string, but they diverge on a bare
 * one: `new Date` reads it as local time, which silently shifts a stored-UTC instant by the
 * reader's offset. Unparseable input sorts as 0 rather than NaN, so a bad row cannot make
 * a list sort arbitrarily.
 */
export function timestampMs(value?: string | null): number {
  return parseTimestamp(value)?.getTime() ?? 0;
}


export function formatDateDDMMYYYY(value: string | null | undefined): string {
  const date = parseTimestamp(value);
  if (!date) return 'Unknown';

  const day = String(date.getDate()).padStart(2, '0');
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const year = date.getFullYear();

  return `${day}/${month}/${year}`;
}

export function formatDateTimeDDMMYYYY(value: string | null | undefined): string {
  const date = parseTimestamp(value);
  if (!date) return 'Unknown';

  const day = String(date.getDate()).padStart(2, '0');
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const year = date.getFullYear();
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');

  return `${day}/${month}/${year} ${hours}:${minutes}`;
}
