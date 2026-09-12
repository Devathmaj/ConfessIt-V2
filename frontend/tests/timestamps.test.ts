import { describe, it, expect } from 'vitest';
import {
  parseTimestamp,
  timestampMs,
  formatDateDDMMYYYY,
  formatDateTimeDDMMYYYY,
} from '@/lib/utils';

/**
 * The wire format carries an explicit offset, so the browser is the only place a local
 * rendering can happen. These tests pin that every entry point agrees on the instant, and
 * that a bare timestamp is read as UTC rather than as the reader's own zone -- the
 * assumption `parseTimestamp` encodes and the server's serializer now guarantees.
 */
describe('parseTimestamp', () => {
  it('reads an offset-carrying timestamp as that instant', () => {
    const parsed = parseTimestamp('2026-09-28T12:01:36+00:00');

    expect(parsed?.toISOString()).toBe('2026-09-28T12:01:36.000Z');
  });

  it('reads a bare timestamp as UTC', () => {
    // Not as local. A bare string with no offset is UTC by contract, which is why the
    // server now always emits one.
    const parsed = parseTimestamp('2026-09-28T12:01:36');

    expect(parsed?.toISOString()).toBe('2026-09-28T12:01:36.000Z');
  });

  it('agrees with a bare timestamp and its explicit UTC form', () => {
    expect(parseTimestamp('2026-09-28T12:01:36')?.getTime()).toBe(
      parseTimestamp('2026-09-28T12:01:36+00:00')?.getTime()
    );
    expect(parseTimestamp('2026-09-28T12:01:36Z')?.getTime()).toBe(
      parseTimestamp('2026-09-28T12:01:36+00:00')?.getTime()
    );
  });

  it('honours a non-UTC offset rather than shifting it', () => {
    // 17:31 at UTC+5:30 is 12:01 UTC -- the same moment, not 17:31 UTC.
    const parsed = parseTimestamp('2026-09-28T17:31:00+05:30');

    expect(parsed?.toISOString()).toBe('2026-09-28T12:01:00.000Z');
  });

  it('returns null rather than an invalid date', () => {
    expect(parseTimestamp('nonsense')).toBeNull();
    expect(parseTimestamp('')).toBeNull();
    expect(parseTimestamp(null)).toBeNull();
    expect(parseTimestamp(undefined)).toBeNull();
  });
});

describe('timestampMs', () => {
  it('agrees with parseTimestamp', () => {
    expect(timestampMs('2026-09-28T12:01:36')).toBe(
      parseTimestamp('2026-09-28T12:01:36')!.getTime()
    );
  });

  it('sorts an unparseable timestamp last instead of producing NaN', () => {
    expect(timestampMs('nonsense')).toBe(0);
    expect(timestampMs(null)).toBe(0);
    // NaN in a comparator makes ordering arbitrary; 0 keeps it stable and oldest-first.
    expect(Number.isNaN(timestampMs('nonsense'))).toBe(false);
  });

  it('orders two conversations by their instants', () => {
    const older = '2026-09-28T10:00:00+00:00';
    const newer = '2026-09-28T12:00:00+00:00';

    expect([older, newer].sort((a, b) => timestampMs(b) - timestampMs(a))).toEqual([
      newer,
      older,
    ]);
  });
});

describe('local rendering', () => {
  it('renders a UTC instant in the browser zone, not in UTC', () => {
    const value = '2026-09-28T12:01:36+00:00';
    const parsed = parseTimestamp(value)!;

    // Built from local getters, exactly as the formatters do, so the assertion holds
    // wherever the suite runs rather than only in UTC.
    const expectedDay = String(parsed.getDate()).padStart(2, '0');
    const expectedMonth = String(parsed.getMonth() + 1).padStart(2, '0');

    expect(formatDateDDMMYYYY(value)).toBe(
      `${expectedDay}/${expectedMonth}/${parsed.getFullYear()}`
    );
  });

  it('formats the date and time from the same instant', () => {
    const value = '2026-09-28T12:01:36+00:00';
    const parsed = parseTimestamp(value)!;
    const hours = String(parsed.getHours()).padStart(2, '0');
    const minutes = String(parsed.getMinutes()).padStart(2, '0');

    expect(formatDateTimeDDMMYYYY(value)).toContain(`${hours}:${minutes}`);
  });

  it('reports a missing or broken timestamp as Unknown, not Invalid date', () => {
    expect(formatDateDDMMYYYY(null)).toBe('Unknown');
    expect(formatDateDDMMYYYY('nonsense')).toBe('Unknown');
    expect(formatDateTimeDDMMYYYY(undefined)).toBe('Unknown');
  });
});
