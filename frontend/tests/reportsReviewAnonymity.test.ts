import { describe, it, expect } from 'vitest';
import { authorLabel } from '@/lib/reportAuthors';

const person = (over: Partial<{ id: string | null; regno: string | null; name: string | null }> = {}) => ({
  id: null,
  regno: null,
  name: null,
  ...over,
});

/**
 * An anonymous confession must stay anonymous on the staff side too. The server keeps the
 * author out of the payload, so these cases are the case where the response changes shape or
 * a new field starts carrying the name -- the screen has to still not print it.
 */
describe('authorLabel', () => {
  it('names the author of a confession that is not anonymous', () => {
    expect(
      authorLabel({
        author: person({ id: 'u1', regno: 'CS-1', name: 'Ada Lovelace' }),
        author_hidden: false,
      })
    ).toBe('Ada Lovelace');
  });

  it('withholds the name of an anonymous author even if a name reaches the payload', () => {
    expect(
      authorLabel({
        author: person({ id: 'u1', regno: 'CS-1', name: 'Ada Lovelace' }),
        author_hidden: true,
      })
    ).toBe('Anonymous to everyone, including staff');
  });

  it('says so rather than printing nothing when an author row is missing', () => {
    // A blank line in a moderation queue reads as a rendering bug; a sentence reads as a fact.
    expect(authorLabel({ author: null, author_hidden: false })).toBe('Unknown');
    expect(authorLabel({ author: null, author_hidden: true })).toBe('Anonymous to everyone, including staff');
  });

  it('falls back to the regno when a named author has no name on file', () => {
    expect(authorLabel({ author: person({ regno: 'CS-7' }), author_hidden: false })).toBe('CS-7');
  });
});
