import { describe, expect, it } from 'vitest';
import { appendBounded, boundMessages } from '@/lib/boundedMessages';

describe('bounded chat windows', () => {
  it('keeps the newest rows when trimming', () => {
    expect(boundMessages([1, 2, 3, 4], 2)).toEqual([3, 4]);
    expect(boundMessages([1], 2)).toEqual([1]);
  });

  it('appends without growing past the ceiling', () => {
    expect(appendBounded([1, 2, 3], 4, 3)).toEqual([2, 3, 4]);
    expect(appendBounded([], 1, 3)).toEqual([1]);
  });
});
