/**
 * Client-side image checks before an upload leaves the browser.
 *
 * The server re-validates everything (format, size, geometry) and is the real
 * gate; these checks exist so a doomed file is refused in milliseconds with a
 * readable sentence instead of after a multi-megabyte upload the server rejects
 * anyway. Limits mirror the backend's `storage_service` defaults
 * (`BASE_ALLOWED_FORMATS`, 2 MB, 6000 px per side, 12M pixels) -- if those move,
 * these messages must move with them.
 */

/** MIME types the backend accepts for uploaded images. */
export const ACCEPTED_IMAGE_MIMES = [
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
] as const;

/** Rejects files larger than this before uploading. Mirrors the backend default. */
export const MAX_UPLOAD_BYTES = 2 * 1024 * 1024;

/** Rejects decoded bitmaps larger than this per side. Mirrors the backend default. */
export const MAX_IMAGE_DIMENSION_PX = 6000;

/** Rejects decoded bitmaps with more pixels than this. Mirrors the backend default. */
export const MAX_IMAGE_PIXELS = 12_000_000;

export const ACCEPT_ATTRIBUTE = 'image/png, image/jpeg, image/gif, image/webp';

/**
 * Returns `null` when `file` is an acceptable image, otherwise a human-readable
 * reason to show instead of uploading.
 *
 * Three layers, cheapest first: the MIME sniff (the `accept` attribute is a hint
 * the picker can bypass), the byte size, then an actual decode via
 * `createImageBitmap`, which rejects polyglots and truncated files that wear an
 * image extension, plus a geometry cap (a tiny file can describe a gigapixel
 * bitmap). Browsers without `createImageBitmap` skip the decode layer; the
 * server still enforces it.
 */
export const validateImageFile = async (
  file: File,
  opts?: { maxBytes?: number },
): Promise<string | null> => {
  if (!ACCEPTED_IMAGE_MIMES.includes(file.type as (typeof ACCEPTED_IMAGE_MIMES)[number])) {
    return 'Unsupported file type. Please choose a PNG, JPEG, GIF or WebP image.';
  }

  const maxBytes = opts?.maxBytes ?? MAX_UPLOAD_BYTES;
  if (file.size > maxBytes) {
    return 'File is too large. Maximum size is 2MB.';
  }

  if (typeof createImageBitmap !== 'function') {
    return null;
  }

  let bitmap: ImageBitmap | null;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    bitmap = null;
  }
  if (!bitmap) {
    return 'That file could not be read as an image. Please choose another file.';
  }
  try {
    if (bitmap.width > MAX_IMAGE_DIMENSION_PX || bitmap.height > MAX_IMAGE_DIMENSION_PX) {
      return `Image dimensions are too large. Maximum side is ${MAX_IMAGE_DIMENSION_PX}px.`;
    }
    if (bitmap.width * bitmap.height > MAX_IMAGE_PIXELS) {
      return 'Image has too many pixels. Please choose a smaller image.';
    }
  } finally {
    bitmap.close();
  }
  return null;
};
