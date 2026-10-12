import {
  ADMIN_BULK_UPLOAD_MAX_BYTES,
  ADMIN_BULK_UPLOAD_MAX_ROWS,
} from '@/lib/limits';

/**
 * Best-effort browser-side preview for the bulk participant upload.
 *
 * The server remains the only validator and the only thing that writes rows. This module
 * exists so an operator can look at a file *before* submitting it without making the backend
 * parse the same file twice. It deliberately does not reproduce the server's business rules
 * (duplicate detection, account/department ceilings, formula policy); it only turns CSV/XLSX
 * bytes into a table of strings. A file that previews cleanly can still be refused by the
 * server, and the dialog says so.
 *
 * The parser is bounded by the same row and byte ceilings as the upload. A preview of a
 * larger file is not useful -- the server refuses it -- and reading one in the tab would be
 * exactly the kind of work this local preview is meant to avoid.
 */

export interface BulkUploadPreview {
  /** Column headings as they appear in the file; unnamed columns get a positional label. */
  headers: string[];
  /** Data rows, padded to the heading width and capped at the upload row limit. */
  rows: string[][];
  /** True when the file has more data rows than the upload accepts. */
  truncated: boolean;
  /** True when the file has more columns than the preview shows. */
  columnsTruncated: boolean;
}

export const BULK_UPLOAD_PREVIEW_PAGE_SIZE = 10;
export const BULK_UPLOAD_PREVIEW_MAX_COLUMNS = 10;

// The browser preview is a new place an operator's tab can meet a workbook from elsewhere.
// These bounds are deliberately lower than anything a legitimate 1000-row file needs, and
// they are read from the ZIP central directory *before* the spreadsheet parser inflates
// anything. The server repeats these checks with its own bound before writing a row; this
// copy exists so a compressed bomb cannot freeze the browser that is only trying to preview.
const XLSX_PREVIEW_MAX_ENTRIES = 2000;
const XLSX_PREVIEW_MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024;
const XLSX_PREVIEW_MAX_COMPRESSION_RATIO = 200;

const SAMPLE_HEADERS = [
  'registration number',
  'email',
  'full name',
  'gender',
  'year',
  'department',
];

const SAMPLE_ROWS = [
  ['23CSE001', 'ada@example.com', 'Ada Lovelace', 'female', '2', 'CSE'],
  ['23ECE002', 'grace@example.com', 'Grace Hopper', 'other', '3', 'ECE'],
];

const CSV_DELIMITERS = [',', ';', '\t'] as const;

function escapeCsvCell(value: string): string {
  return /[",\r\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

/**
 * A template with exactly the columns the endpoint accepts.
 *
 * CSV rather than XLSX because it is a text file the browser can produce without a writer
 * dependency, and every spreadsheet application opens it. The download adds a UTF-8 BOM so
 * Excel detects the encoding instead of guessing the local code page.
 */
export function buildBulkUploadSampleCsv(): string {
  return (
    [SAMPLE_HEADERS, ...SAMPLE_ROWS]
      .map((row) => row.map(escapeCsvCell).join(','))
      .join('\r\n') + '\r\n'
  );
}

export function downloadBulkUploadSample(): void {
  const blob = new Blob(['\ufeff', buildBulkUploadSampleCsv()], {
    type: 'text/csv;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'participants-sample.csv';
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoking immediately after click is allowed, but deferring one turn is friendlier to
  // browsers that begin the download asynchronously.
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function detectDelimiter(text: string): string {
  const firstLine = text.split(/\r?\n/).find((line) => line.trim() !== '') ?? '';
  let best = ',';
  let bestCount = 0;
  for (const delimiter of CSV_DELIMITERS) {
    const count = firstLine.split(delimiter).length - 1;
    if (count > bestCount) {
      best = delimiter;
      bestCount = count;
    }
  }
  return best;
}

/**
 * Split delimited text into rows.
 *
 * Handles quoted fields, escaped quotes, CRLF/LF, and the three delimiters the server's CSV
 * reader recognises. `maxRows` is a safety bound: once it is reached, the caller has enough
 * to know the file is over the upload limit, and continuing to parse cannot change the
 * preview.
 */
export function parseCsvRows(
  text: string,
  maxRows = ADMIN_BULK_UPLOAD_MAX_ROWS + 2,
): string[][] {
  const source = text.replace(/^\uFEFF/, '');
  const delimiter = detectDelimiter(source);
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let inQuotes = false;

  const finishRow = () => {
    row.push(field);
    rows.push(row);
    row = [];
    field = '';
  };

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];

    if (inQuotes) {
      if (character === '"') {
        if (source[index + 1] === '"') {
          field += '"';
          index += 1;
        } else {
          inQuotes = false;
        }
      } else {
        field += character;
      }
      continue;
    }

    if (character === '"' && field.length === 0) {
      inQuotes = true;
      continue;
    }
    if (character === delimiter) {
      row.push(field);
      field = '';
      continue;
    }
    if (character === '\n' || character === '\r') {
      if (character === '\r' && source[index + 1] === '\n') index += 1;
      finishRow();
      if (rows.length >= maxRows) return rows;
      continue;
    }
    field += character;
  }

  if (field.length > 0 || row.length > 0) finishRow();
  return rows;
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (value instanceof Date) return value.toISOString().slice(0, 10);
  if (typeof value === 'object') return '';
  return String(value);
}

function normalizeRow(row: readonly unknown[]): string[] {
  const values = row.map(formatCell);
  // Spreadsheet formatting often extends a row with many empty trailing cells. They are not
  // part of the operator's data, so they should not become columns in the preview.
  while (values.length > 0 && values[values.length - 1].trim() === '') {
    values.pop();
  }
  return values;
}

/**
 * Turn parsed rows into the display model.
 *
 * Blank rows are ignored (the server does the same), the first non-blank row becomes the
 * headings, and data is capped at the upload row limit. The preview does not try to decide
 * whether the headings are a supported set -- that is a server rule; showing exactly what the
 * file contains is more useful when diagnosing a refusal.
 */
export function previewFromRows(rows: readonly (readonly unknown[])[]): BulkUploadPreview {
  const cleaned = rows
    .map(normalizeRow)
    .filter((row) => row.some((cell) => cell.trim() !== ''));

  if (cleaned.length === 0) {
    return { headers: [], rows: [], truncated: false, columnsTruncated: false };
  }

  const widestRow = cleaned.reduce((width, row) => Math.max(width, row.length), 0);
  const width = Math.min(widestRow, BULK_UPLOAD_PREVIEW_MAX_COLUMNS);
  const columnsTruncated = widestRow > width;
  const headingRow = cleaned[0] ?? [];
  const headers = Array.from({ length: width }, (_, index) => {
    const heading = (headingRow[index] ?? '').trim();
    return heading || `Column ${index + 1}`;
  });
  const dataRows = cleaned.slice(1);
  const shownRows = dataRows.slice(0, ADMIN_BULK_UPLOAD_MAX_ROWS).map((row) =>
    Array.from({ length: width }, (_, index) => row[index] ?? ''),
  );

  return {
    headers,
    rows: shownRows,
    truncated: dataRows.length > ADMIN_BULK_UPLOAD_MAX_ROWS,
    columnsTruncated,
  };
}

function xlsxPreviewError(message: string): Error {
  return new Error(
    `${message} You can still upload it; the server will validate it and report any problem.`,
  );
}

/**
 * Inspect an XLSX ZIP container without inflating it.
 *
 * This is the browser-side counterpart of the server's archive checks, narrowed to the risk
 * that matters here: a small workbook that expands into an enormous one while the tab is
 * merely trying to show a preview. The central directory advertises the sizes, so they can be
 * summed before the parser reads any member.
 */
export function inspectXlsxArchive(buffer: ArrayBuffer): void {
  const bytes = new Uint8Array(buffer);
  if (
    bytes.length < 22 ||
    bytes[0] !== 0x50 ||
    bytes[1] !== 0x4b ||
    bytes[2] !== 0x03 ||
    bytes[3] !== 0x04
  ) {
    throw xlsxPreviewError('The file is named .xlsx but is not a ZIP workbook.');
  }

  const view = new DataView(buffer);
  const endOfFile = bytes.length;
  const firstPossibleEocd = Math.max(0, endOfFile - 22 - 0xffff);
  let eocdOffset = -1;
  for (let offset = endOfFile - 22; offset >= firstPossibleEocd; offset -= 1) {
    if (view.getUint32(offset, true) === 0x06054b50) {
      eocdOffset = offset;
      break;
    }
  }
  if (eocdOffset < 0) {
    throw xlsxPreviewError('The XLSX workbook has no readable ZIP directory.');
  }

  const diskNumber = view.getUint16(eocdOffset + 4, true);
  const centralDirectoryDisk = view.getUint16(eocdOffset + 6, true);
  const entriesOnDisk = view.getUint16(eocdOffset + 8, true);
  const entryCount = view.getUint16(eocdOffset + 10, true);
  const centralDirectorySize = view.getUint32(eocdOffset + 12, true);
  const centralDirectoryOffset = view.getUint32(eocdOffset + 16, true);

  if (
    diskNumber !== 0 ||
    centralDirectoryDisk !== 0 ||
    entriesOnDisk !== entryCount ||
    entryCount === 0xffff ||
    centralDirectorySize === 0xffffffff ||
    centralDirectoryOffset === 0xffffffff
  ) {
    throw xlsxPreviewError(
      'The XLSX workbook uses a ZIP64 or multi-disk container, which the preview does not read.',
    );
  }
  if (
    entryCount > XLSX_PREVIEW_MAX_ENTRIES ||
    centralDirectoryOffset + centralDirectorySize > endOfFile
  ) {
    throw xlsxPreviewError('The XLSX workbook has an unreasonable ZIP directory.');
  }

  let totalUncompressed = 0;
  let offset = centralDirectoryOffset;
  for (let index = 0; index < entryCount; index += 1) {
    if (offset + 46 > endOfFile || view.getUint32(offset, true) !== 0x02014b50) {
      throw xlsxPreviewError('The XLSX workbook has a malformed ZIP directory.');
    }
    const flags = view.getUint16(offset + 8, true);
    const compressedSize = view.getUint32(offset + 20, true);
    const uncompressedSize = view.getUint32(offset + 24, true);
    const nameLength = view.getUint16(offset + 28, true);
    const extraLength = view.getUint16(offset + 30, true);
    const commentLength = view.getUint16(offset + 32, true);

    if (flags & 0x1) {
      throw xlsxPreviewError('The XLSX workbook contains an encrypted part.');
    }
    if (compressedSize === 0xffffffff || uncompressedSize === 0xffffffff) {
      throw xlsxPreviewError('The XLSX workbook uses ZIP64 entry sizes.');
    }

    totalUncompressed += uncompressedSize;
    if (totalUncompressed > XLSX_PREVIEW_MAX_UNCOMPRESSED_BYTES) {
      throw xlsxPreviewError(
        'The XLSX workbook expands to more data than can be previewed safely.',
      );
    }
    if (
      uncompressedSize > 1024 * 1024 &&
      compressedSize > 0 &&
      uncompressedSize / compressedSize > XLSX_PREVIEW_MAX_COMPRESSION_RATIO
    ) {
      throw xlsxPreviewError('The XLSX workbook has an unsafe compression ratio.');
    }

    offset += 46 + nameLength + extraLength + commentLength;
    if (offset > centralDirectoryOffset + centralDirectorySize) {
      throw xlsxPreviewError('The XLSX workbook has a malformed ZIP directory.');
    }
  }
}

/**
 * Read a selected file in the browser for preview.
 *
 * XLSX parsing is loaded on demand so the profile-review page does not pay for a spreadsheet
 * parser until somebody actually selects a workbook. `.xlsx` is the only workbook extension
 * offered: macro-enabled and legacy formats are refused by the upload endpoint.
 */
export async function parseBulkUploadPreview(file: File): Promise<BulkUploadPreview> {
  if (file.size > ADMIN_BULK_UPLOAD_MAX_BYTES) {
    throw new Error(
      `This file is larger than the ${ADMIN_BULK_UPLOAD_MAX_BYTES / (1024 * 1024)} MB limit, ` +
        'so it cannot be previewed or uploaded.',
    );
  }

  const name = file.name.toLowerCase();

  if (name.endsWith('.csv')) {
    return previewFromRows(parseCsvRows(await file.text()));
  }

  if (name.endsWith('.xlsx')) {
    let buffer: ArrayBuffer;
    try {
      buffer = await file.arrayBuffer();
    } catch {
      throw xlsxPreviewError('The XLSX file could not be read.');
    }

    // This runs before the spreadsheet parser sees the bytes. It throws its own user-facing
    // errors, so it is deliberately outside the parser's catch below.
    inspectXlsxArchive(buffer);

    try {
      // `/universal` avoids the package's Web Worker wrapper. At the upload ceiling of 1000
      // rows this is a short operation, and it keeps the preview path working in the same
      // environment as the rest of the page (including component tests).
      const { readSheet } = await import('read-excel-file/universal');
      const rows = await readSheet(buffer);
      return previewFromRows(rows);
    } catch {
      throw xlsxPreviewError('The XLSX file could not be read for preview.');
    }
  }

  throw new Error('Only .csv and .xlsx files can be previewed.');
}
