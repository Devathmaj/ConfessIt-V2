import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  BULK_UPLOAD_PREVIEW_MAX_COLUMNS,
  buildBulkUploadSampleCsv,
  inspectXlsxArchive,
  parseBulkUploadPreview,
  parseCsvRows,
  previewFromRows,
} from '@/lib/bulkUploadPreview';
import { ADMIN_BULK_UPLOAD_MAX_ROWS } from '@/lib/limits';

/**
 * The browser-side preview is not a validator, but it still has to read the formats the
 * upload accepts without mangling the operator's data. These tests cover the parsing pieces
 * that are easy to get subtly wrong -- quoted commas, embedded newlines, delimiter choice,
 * blank rows, the row cap and the column cap -- independently of the modal that renders them.
 */

const HEADER = 'registration number,email,full name,gender,year,department';

describe('bulk upload preview parsing', () => {
  it('reads the downloadable sample as the six expected columns', () => {
    const preview = previewFromRows(parseCsvRows(buildBulkUploadSampleCsv()));

    expect(preview.headers).toEqual([
      'registration number',
      'email',
      'full name',
      'gender',
      'year',
      'department',
    ]);
    expect(preview.rows).toHaveLength(2);
    expect(preview.truncated).toBe(false);
    expect(preview.columnsTruncated).toBe(false);
  });

  it('keeps quoted commas and embedded newlines inside one cell', () => {
    const rows = parseCsvRows(
      `${HEADER}\r\n"23,001",ada@example.com,"Doe, Jane\nSecond line",female,2,CSE\r\n`,
    );

    expect(rows).toHaveLength(2);
    expect(rows[1][0]).toBe('23,001');
    expect(rows[1][2]).toBe('Doe, Jane\nSecond line');
  });

  it('detects a semicolon-delimited export', () => {
    const rows = parseCsvRows(
      'registration number;email;full name;gender;year;department\n' +
        'sample-1;sample@example.com;Sample Person;other;2;CSE\n',
    );

    expect(rows[0]).toEqual([
      'registration number',
      'email',
      'full name',
      'gender',
      'year',
      'department',
    ]);
    expect(rows[1][2]).toBe('Sample Person');
  });

  it('ignores a UTF-8 BOM and blank rows around the table', () => {
    const preview = previewFromRows(
      parseCsvRows(`\uFEFF${HEADER}\r\n\r\nsample-1,sample@example.com,Sample,other,2,CSE\r\n\r\n`),
    );

    expect(preview.headers[0]).toBe('registration number');
    expect(preview.rows).toEqual([
      ['sample-1', 'sample@example.com', 'Sample', 'other', '2', 'CSE'],
    ]);
  });

  it('caps the preview at the upload row limit but reports that it stopped', () => {
    const rows = [
      HEADER,
      ...Array.from(
        { length: ADMIN_BULK_UPLOAD_MAX_ROWS + 1 },
        (_, index) => `row-${index},row-${index}@example.com,Row ${index},other,2,CSE`,
      ),
    ];

    const preview = previewFromRows(parseCsvRows(rows.join('\n')));

    expect(preview.rows).toHaveLength(ADMIN_BULK_UPLOAD_MAX_ROWS);
    expect(preview.truncated).toBe(true);
  });

  it('caps absurdly wide files so a malformed export cannot build a huge table', () => {
    const heading = Array.from({ length: BULK_UPLOAD_PREVIEW_MAX_COLUMNS + 3 }, (_, i) => `h${i}`);
    const value = Array.from({ length: BULK_UPLOAD_PREVIEW_MAX_COLUMNS + 3 }, (_, i) => `v${i}`);
    const preview = previewFromRows(parseCsvRows(`${heading.join(',')}\n${value.join(',')}`));

    expect(preview.headers).toHaveLength(BULK_UPLOAD_PREVIEW_MAX_COLUMNS);
    expect(preview.rows[0]).toHaveLength(BULK_UPLOAD_PREVIEW_MAX_COLUMNS);
    expect(preview.columnsTruncated).toBe(true);
  });

  it('returns an empty preview for an empty file', () => {
    expect(previewFromRows(parseCsvRows(''))).toEqual({
      headers: [],
      rows: [],
      truncated: false,
      columnsTruncated: false,
    });
  });
});

function zipWithAdvertisedEntry(
  uncompressedSize: number,
  compressedSize: number,
): ArrayBuffer {
  // A central directory and EOCD are all the browser-side guard reads. Building only those
  // lets the test describe an enormous decompression without allocating or compressing it.
  const centralDirectoryOffset = 4;
  const buffer = new ArrayBuffer(centralDirectoryOffset + 46 + 22);
  const view = new DataView(buffer);
  new Uint8Array(buffer).set([0x50, 0x4b, 0x03, 0x04], 0);

  view.setUint32(centralDirectoryOffset, 0x02014b50, true);
  view.setUint16(centralDirectoryOffset + 8, 0, true);
  view.setUint32(centralDirectoryOffset + 20, compressedSize, true);
  view.setUint32(centralDirectoryOffset + 24, uncompressedSize, true);

  const eocdOffset = centralDirectoryOffset + 46;
  view.setUint32(eocdOffset, 0x06054b50, true);
  view.setUint16(eocdOffset + 8, 1, true);
  view.setUint16(eocdOffset + 10, 1, true);
  view.setUint32(eocdOffset + 12, 46, true);
  view.setUint32(eocdOffset + 16, centralDirectoryOffset, true);
  return buffer;
}

describe('bulk upload preview XLSX', () => {
  it('refuses an archive that advertises more data than the preview will inflate', () => {
    expect(() => inspectXlsxArchive(zipWithAdvertisedEntry(30 * 1024 * 1024, 1024))).toThrow(
      /expands to more data/i,
    );
  });

  it('refuses a fake ZIP that is not an XLSX container', () => {
    expect(() => inspectXlsxArchive(new Uint8Array([1, 2, 3, 4]).buffer)).toThrow(
      /not a ZIP workbook/i,
    );
  });

  it('reads a real workbook through the browser parser', async () => {
    const bytes = readFileSync(
      resolve(process.cwd(), 'tests/fixtures/bulk-upload-participants.xlsx'),
    );
    const file = new File([bytes], 'participants.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    });

    const preview = await parseBulkUploadPreview(file);

    expect(preview.headers).toEqual([
      'registration number',
      'email',
      'full name',
      'gender',
      'year',
      'department',
    ]);
    expect(preview.rows).toEqual([
      ['xlsx-preview-1', 'xlsx1@example.com', 'Xlsx Preview One', 'female', '2', 'CSE'],
      ['xlsx-preview-2', 'xlsx2@example.com', 'Xlsx Preview Two', 'other', '3', 'ECE'],
    ]);
    expect(preview.truncated).toBe(false);
  });
});
