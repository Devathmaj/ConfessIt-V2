import { describe, it, expect } from 'vitest';
import { resolveProfilePictureUrl } from '@/lib/utils';

/**
 * The avatar URL allowlist.
 *
 * `<img src>` executes SVG, so this is a strict allowlist rather than a scheme check:
 * the Cloudinary delivery host, raster-only `data:` images (never SVG, never HTML),
 * and site-relative paths. Everything else -- cleartext `http:`, `javascript:`,
 * lookalike hosts, SVG payloads -- degrades to the placeholder tile, never to code.
 */
describe('resolveProfilePictureUrl', () => {
  it('passes the Cloudinary delivery host through', () => {
    expect(
      resolveProfilePictureUrl('https://res.cloudinary.com/demo/image/upload/pic.png'),
    ).toBe('https://res.cloudinary.com/demo/image/upload/pic.png');
  });

  it('passes raster inline images through, SVG never', () => {
    const png = 'data:image/png;base64,iVBORw0KGgo=';
    expect(resolveProfilePictureUrl(png)).toBe(png);
    expect(resolveProfilePictureUrl('DATA:IMAGE/WEBP;BASE64,AAA')).toBe(
      'DATA:IMAGE/WEBP;BASE64,AAA',
    );
    expect(resolveProfilePictureUrl('data:image/svg+xml,<svg onload=alert(1)>')).toBe(
      '/placeholder.svg',
    );
    expect(resolveProfilePictureUrl('data:image/svg+xml;base64,PHN2Zz4=')).toBe(
      '/placeholder.svg',
    );
    expect(resolveProfilePictureUrl('data:text/html,<script>alert(1)</script>')).toBe(
      '/placeholder.svg',
    );
    // A non-base64 raster payload could still be sniffed; only ;base64 passes.
    expect(resolveProfilePictureUrl('data:image/png,<svg onload=alert(1)>')).toBe(
      '/placeholder.svg',
    );
  });

  it('passes site-relative paths through', () => {
    expect(resolveProfilePictureUrl('/placeholder.svg')).toBe('/placeholder.svg');
    expect(resolveProfilePictureUrl('/templates/sunset.png')).toBe('/templates/sunset.png');
  });

  it('falls back for missing, remote, cleartext and scriptable values', () => {
    expect(resolveProfilePictureUrl(null)).toBe('/placeholder.svg');
    expect(resolveProfilePictureUrl(undefined)).toBe('/placeholder.svg');
    expect(resolveProfilePictureUrl('')).toBe('/placeholder.svg');
    expect(resolveProfilePictureUrl('http://res.cloudinary.com/x/y.png')).toBe(
      '/placeholder.svg',
    );
    expect(resolveProfilePictureUrl('https://evil.example.com/pic.png')).toBe(
      '/placeholder.svg',
    );
    expect(resolveProfilePictureUrl('https://evilcloudinary.com/pic.png')).toBe(
      '/placeholder.svg',
    );
    expect(resolveProfilePictureUrl('https://cloudinary.com.evil.com/pic.png')).toBe(
      '/placeholder.svg',
    );
    expect(resolveProfilePictureUrl('javascript:alert(1)')).toBe('/placeholder.svg');
  });
});
