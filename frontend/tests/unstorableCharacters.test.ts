import { describe, it, expect } from 'vitest';
import { containsUnstorableCharacter, CONFESSION_BODY_MAX } from '@/lib/limits';

/**
 * Mirrors the server rule in backend/app/models.py. The browser check exists so a user is
 * told while their text is still on screen; these cases pin down the split between the two
 * things that look like one rule and are not.
 *
 * The dangerous failure mode for a filter like this is over-reach, so the "accepts" cases
 * are the ones that matter most. A pattern that refuses Devanagari or emoji works perfectly
 * as a character filter and ruins the feature.
 *
 * The refused characters are built with String.fromCharCode rather than written as escapes or
 * literals on purpose. A literal control character in a source file is invisible, survives
 * copy-paste intact, and makes the test itself the thing that is hard to read; a `\u0000` that
 * some tool rewrites into a real byte has the same problem. Naming the code point is the only
 * version of this that still reads correctly next year.
 */
const at = (codePoint: number) => String.fromCharCode(codePoint);

describe('containsUnstorableCharacter', () => {
  it.each([
    ['NUL byte', 0x00],
    ['vertical tab', 0x0b],
    ['form feed', 0x0c],
    ['backspace', 0x08],
    ['escape', 0x1b],
    ['bell', 0x07],
    ['delete', 0x7f],
    ['first C1 control', 0x9b],
    ['last C1 control', 0x9f],
    ['U+FFFE non-character', 0xfffe],
    ['U+FFFF non-character', 0xffff],
  ])('refuses %s', (_label, codePoint) => {
    expect(containsUnstorableCharacter(at(codePoint))).toBe(true);
  });

  it.each([
    ['U+FDD0', 0xfdd0],
    ['U+FDEF', 0xfdef],
    // The pair the BMP tests do not cover and that a check written for the BMP alone misses.
    ['U+1FFFE', 0x1fffe],
    ['U+1FFFF', 0x1ffff],
    ['U+10FFFE', 0x10fffe],
    ['U+10FFFF', 0x10ffff],
  ])('refuses the non-character %s in an astral plane', (_label, codePoint) => {
    expect(containsUnstorableCharacter(String.fromCodePoint(codePoint))).toBe(true);
  });

  it.each([
    ['U+FDF0', 0xfdf0],
    ['U+FFFD, the replacement character', 0xfffd],
    ['U+FDCF, below the non-character block', 0xfdcf],
    // The assignable neighbours of the astral non-characters. If the rule ever widens by one
    // step these are the characters it takes with it, which is why they are named.
    ['U+1FFFD', 0x1fffd],
    ['U+10FFFD', 0x10fffd],
  ])('accepts %s', (_label, codePoint) => {
    expect(containsUnstorableCharacter(String.fromCodePoint(codePoint))).toBe(false);
  });

  it('refuses every control character in the C0 block except the three that are whitespace', () => {
    // Whitespace is not a fault in the input: a confession is several lines of prose and the
    // three characters a person can produce with a keyboard have to get through.
    const whitespace = new Set([0x09, 0x0a, 0x0d]);

    for (let codePoint = 0x00; codePoint <= 0x1f; codePoint += 1) {
      const expected = !whitespace.has(codePoint);
      expect(containsUnstorableCharacter(at(codePoint)), `U+${codePoint.toString(16)}`).toBe(expected);
    }
  });

  it.each([
    ['plain ascii', 'I still think about you'],
    ['emoji', 'hi 💕 there'],
    ['ZWJ emoji sequence', '👨‍👩'],
    ['devanagari', 'मेरे है'],
    ['tamil', 'நீ நன்றாக இரு'],
    ['arabic', 'أشتاق إليك'],
    ['arabic with diacritics', 'أَشْتَاقُ'],
    ['chinese', '我想你了'],
    ['japanese', 'そばにいてください'],
    ['korean', '보고 싶어요'],
    // The right-to-left override characters are how Arabic and Hebrew are written, so a filter
    // that refused them would refuse the people least likely to write in English.
    ['RTL override marks', 'hello ‮abc‬ world'],
    ['newlines and tabs', 'line one\nline two\ttabbed'],
    ['carriage return', 'one\r\ntwo'],
    ['combining diacritics', 'éàü'],
    ['variation selector', '❤️'],
    ['zero width joiner', '👩‍🚀'],
    ['non-breaking space', 'one two'],
    ['astral plane', '𐎠'],
    // The leading surrogates that begin the astral non-characters also begin thousands of
    // real letters. A rule that matched either surrogate on its own would take Deseret,
    // CJK-extension and emoji with it, so these are here to prove they do not.
    ['deseret', '\u{10400}\u{10401}'],
    ['deseret at U+1FC00, next to U+1FFFE', '\u{1FC00}'],
    ['cjk extension B', '\u{20000}'],
  ])('accepts %s', (_label, text) => {
    expect(containsUnstorableCharacter(text)).toBe(false);
  });

  it('finds a bad character anywhere in the text, not only at the start', () => {
    const bell = at(0x07);
    expect(containsUnstorableCharacter(`a perfectly fine confession${bell}`)).toBe(true);
    expect(containsUnstorableCharacter(`${bell}at the very start`)).toBe(true);
    expect(containsUnstorableCharacter(`at the very end${bell}`)).toBe(true);
    expect(containsUnstorableCharacter(`middle${bell}of it`)).toBe(true);
  });

  it('is not stateful across calls', () => {
    // A /g flag would make .test() alternate true and false on the same input, so the second
    // paste of the same text would be accepted.
    const bell = at(0x07);

    expect(containsUnstorableCharacter(`bell${bell}`)).toBe(true);
    expect(containsUnstorableCharacter(`bell${bell}`)).toBe(true);
  });

  it('does not report an empty confession as unstorable', () => {
    // A blank confession fails the non-empty check, not this one; the point is that an empty
    // string must not be reported as containing an unstorable character.
    expect(containsUnstorableCharacter('')).toBe(false);
  });

  it('accepts a confession that is exactly at the length limit', () => {
    // Reading the constant rather than hardcoding a number. When the limit moved to 500 this
    // still passed, because it was checking 2,000 characters against a predicate that has no
    // opinion about length either way -- it stopped testing the limit at all.
    expect(containsUnstorableCharacter('a'.repeat(CONFESSION_BODY_MAX))).toBe(false);
  });
});
