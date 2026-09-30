import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  ACCEPT_ATTRIBUTE,
  ACCEPTED_IMAGE_MIMES,
  MAX_UPLOAD_BYTES,
  validateImageFile,
} from '@/lib/imageFile';

/**
 * Client-side image checks before an upload leaves the browser.
 *
 * The `accept` attribute is a picker hint any file sails past, so the MIME
 * sniff, the byte cap and the real decode all live here too -- with the server
 * as the final gate. `createImageBitmap` is stubbed per case because jsdom has
 * none; the unstubbed cases pin the MIME/size layers and the no-bitmap guard.
 */

const png = (size = 100, type = 'image/png') => new File([new Uint8Array(size)], 'a.png', { type });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('validateImageFile', () => {
  it('rejects MIME types outside the backend allowlist', async () => {
    expect(await validateImageFile(new File(['x'], 'a.svg', { type: 'image/svg+xml' }))).toMatch(
      /Unsupported file type/,
    );
    expect(await validateImageFile(new File(['x'], 'a.pdf', { type: 'application/pdf' }))).toMatch(
      /Unsupported file type/,
    );
    // Every backend-accepted format passes the sniff layer (decode stubbed below).
    for (const type of ACCEPTED_IMAGE_MIMES) {
      vi.stubGlobal(
        'createImageBitmap',
        vi.fn(async () => ({ width: 10, height: 10, close: vi.fn() })),
      );
      expect(await validateImageFile(png(100, type))).toBeNull();
    }
  });

  it('rejects oversize files before decoding', async () => {
    const decode = vi.fn(async () => ({ width: 10, height: 10, close: vi.fn() }));
    vi.stubGlobal('createImageBitmap', decode);
    expect(await validateImageFile(png(MAX_UPLOAD_BYTES + 1))).toMatch(/too large/);
    expect(decode).not.toHaveBeenCalled();
    expect(await validateImageFile(png(MAX_UPLOAD_BYTES))).toBeNull();
  });

  it('rejects files that do not decode as images', async () => {
    vi.stubGlobal(
      'createImageBitmap',
      vi.fn(async () => {
        throw new DOMException('could not decode');
      }),
    );
    expect(await validateImageFile(png())).toMatch(/could not be read as an image/);
  });

  it('rejects absurd geometry, including the small-file huge-bitmap case', async () => {
    const close = vi.fn();
    vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ width: 7000, height: 10, close })));
    expect(await validateImageFile(png())).toMatch(/dimensions are too large/);
    expect(close).toHaveBeenCalled();

    vi.stubGlobal(
      'createImageBitmap',
      vi.fn(async () => ({ width: 4000, height: 4000, close: vi.fn() })),
    );
    expect(await validateImageFile(png())).toMatch(/too many pixels/);
  });

  it('closes the bitmap it decoded', async () => {
    const close = vi.fn();
    vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ width: 10, height: 10, close })));
    expect(await validateImageFile(png())).toBeNull();
    expect(close).toHaveBeenCalled();
  });

  it('skips the decode layer where createImageBitmap does not exist', async () => {
    vi.stubGlobal('createImageBitmap', undefined as never);
    expect(await validateImageFile(png())).toBeNull();
  });

  it('advertises exactly the backend allowlist in the picker hint', () => {
    for (const mime of ACCEPTED_IMAGE_MIMES) {
      expect(ACCEPT_ATTRIBUTE).toContain(mime);
    }
  });
});
