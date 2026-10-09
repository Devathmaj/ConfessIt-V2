/**
 * Fitting a love note's message into the rectangle the template reserves for it.
 *
 * A love note is not text in a box: it is text rasterised onto a fixed PNG. The sender picks a
 * template, a font and a style, and the message is drawn into the `body` field's rectangle at
 * whatever size the template JSON declares. That size is a single number, so it was right for
 * exactly one message length -- and `wrapText` handled every other length by cutting the
 * overflow off with an ellipsis. A note of 300 characters was drawn whole and a note of 500
 * quietly lost its last line, with nothing on the page to say so.
 *
 * So the size is fitted rather than fixed. `MAX_FONT_SIZE` is the *ceiling*: a short note is set
 * in it on every card, and the size only comes down when the message genuinely does not fit the
 * box. That direction matters. Nothing is ever drawn above the ceiling, however few words it
 * has -- see `MAX_FONT_SIZE` for why that is one moderate number rather than a fraction of the
 * card.
 *
 * Fitting is not the same as being able to read it, and the two limits here are not the same
 * kind of thing. `LOVE_NOTE_BODY_MAX` on the server is the one that keeps real messages
 * legible -- at that length the tightest template still fits in roughly 16px -- and this
 * module never sees a longer message through the UI. `MIN_FONT_SIZE` is not a legibility
 * claim at all: it is the floor below which the search stops, so that `fitTextToBox` always
 * terminates and never hands back a size that overflows the card. Truncation is therefore
 * reachable only for a message the server would have refused anyway, and for a single
 * unbreakable run of characters wider than the box.
 */

/** The rectangle a template reserves for one field, in the PNG's own pixels. */
export interface TextBox {
    x: number;
    y: number;
    width: number;
    height: number;
}

/** The style/weight/family a message is set in. The order matches the CSS font shorthand. */
export interface FontStyle {
    style: string;
    weight: string;
    family: string;
}

/**
 * The smallest a message is ever drawn at.
 *
 * 8px on a ~1900px-wide template is small but not invisible. Deliberately not claimed to be
 * readable: see the note above about which limit actually carries that.
 */
export const MIN_FONT_SIZE = 8;

/** What a fit produced: the lines to draw, and the size they were fitted at. */
export interface FittedText {
    lines: string[];
    fontSize: number;
    /**
     * Whether lines had to be dropped to fit even at `MIN_FONT_SIZE`.
     *
     * Not an error condition -- it is the honest report that this message is longer than this
     * card can show at any size this is willing to draw. The caller decides whether that is
     * worth telling the sender about.
     */
    truncated: boolean;
}

/**
 * The `font` shorthand for a size, in the order the canvas API expects.
 *
 * Exported because the caller has to set `ctx.font` before `fillText` as well as before
 * `measureText`, and the fitted size is only known after measuring.
 */
export const fontString = (font: FontStyle, size: number): string =>
    `${font.style} ${font.weight} ${size}px ${font.family}`;

/**
 * The four ways the sender can weight and slant the message.
 *
 * Modelled as one value with four cases rather than two independent toggles for bold and italic,
 * because the question asked was "bold, italic, both, or neither" and two toggles have a fifth
 * answer nobody wants: bold *and* normal, which is the same drawing as bold plus a contradictory
 * setting. Four exclusive cases make that state unrepresentable rather than merely discouraged.
 */
export type StylePreset = 'normal' | 'bold' | 'italic' | 'bold-italic';

/**
 * `label` is what the picker shows and `preview` is what the button is drawn in, so the swatch
 * previews the style it selects instead of describing it in words.
 */
export const STYLE_PRESETS: readonly {
    value: StylePreset;
    label: string;
    preview: { style: 'normal' | 'italic'; weight: 'normal' | 'bold' };
}[] = [
    { value: 'normal', label: 'Normal', preview: { style: 'normal', weight: 'normal' } },
    { value: 'bold', label: 'Bold', preview: { style: 'normal', weight: 'bold' } },
    { value: 'italic', label: 'Italic', preview: { style: 'italic', weight: 'normal' } },
    { value: 'bold-italic', label: 'Bold Italic', preview: { style: 'italic', weight: 'bold' } },
];

/** The CSS slant and weight a preset means, leaving the family to the caller. */
export const fontForPreset = (
    preset: StylePreset
): { style: 'normal' | 'italic'; weight: 'normal' | 'bold' } =>
    STYLE_PRESETS.find((entry) => entry.value === preset)?.preview ?? {
        style: 'normal',
        weight: 'normal',
    };

/**
 * Break `text` into lines no wider than `maxWidth`, measured at whatever font is current.
 *
 * Two passes, because one is not enough. Words are packed greedily first, which is what makes
 * the result read like prose rather than like a fixed-width dump. A word still too wide on its
 * own -- a URL, a run of emoji, a Devanagari compound -- is then split by character, because
 * the alternative is a line that overflows the card, and an overflowing line is the one
 * failure this whole module exists to remove.
 *
 * `measureText` is passed in rather than reached for, so this is a pure function of the text,
 * the width and whatever font the caller has set. jsdom has no text metrics at all, so a
 * `CanvasRenderingContext2D` here would make every assertion about this vacuous.
 */
export const wrapText = (
    measureText: (text: string) => number,
    text: string,
    maxWidth: number
): string[] => {
    const lines: string[] = [];

    for (const paragraph of text.split('\n')) {
        // An empty paragraph still pushes a line, so a blank line the sender typed stays blank
        // in the note instead of silently disappearing from it.
        let currentLine = '';
        for (const word of paragraph.split(' ')) {
            const candidate = currentLine ? `${currentLine} ${word}` : word;
            if (currentLine && measureText(candidate) > maxWidth) {
                lines.push(currentLine);
                currentLine = word;
            } else {
                currentLine = candidate;
            }
        }
        lines.push(currentLine);
    }

    const wrapped: string[] = [];
    for (const line of lines) {
        if (measureText(line) <= maxWidth) {
            wrapped.push(line);
            continue;
        }

        let currentLine = '';
        // By code point, not by UTF-16 unit. An emoji outside the BMP is two units, and
        // splitting between them would put half a surrogate on the card.
        for (const character of line) {
            const candidate = currentLine + character;
            if (currentLine && measureText(candidate) > maxWidth) {
                wrapped.push(currentLine);
                currentLine = character;
            } else {
                currentLine = candidate;
            }
        }
        wrapped.push(currentLine);
    }

    return wrapped;
};

/**
 * The largest size a message is ever drawn at.
 *
 * One number, the same on every card, because the amount of text is what should make a note
 * smaller -- not the artwork behind it. The ceiling used to be a fraction of the box height,
 * which on the tallest template (830x800) came to 320px: two words were laid out as a
 * billboard that dominated the card rather than a message sitting on it, and the same two
 * words were a different size on every template. The templates cannot express a tuned value
 * either -- every one of them shipped the same `body.font.size: 32`, a placeholder that read
 * as tiny. 62px on the 1920x1080 card is 3.2% of its width: clearly larger than that
 * placeholder, and small enough that a short note reads as a note. A longer message is
 * unaffected by this number -- it shrinks below it exactly as before, and only when it cannot
 * fit the box at 62.
 */
export const MAX_FONT_SIZE = 62;

/**
 * The largest size, at or below `maxFontSize`, at which all of `text` fits inside `box`.
 *
 * Searches upwards and stops at the first size that fits, so the result is the largest one that
 * works. Line height is the font size itself, not a multiple of it: the renderer this replaces
 * passed the font size as the line height, so the spacing is unchanged by anything here.
 *
 * The search is a binary search, which needs the fit to be monotonic in the size, and it is. Line
 * count never *decreases* as the size grows -- a larger font fits fewer characters on a line of
 * fixed width, so greedy wrapping breaks at least as often -- and the size factor itself only
 * grows. So `lines * size` is non-decreasing, the sizes that fit are a prefix, and the midpoint
 * rule below converges on the exact largest one. That matters for cost: a box can be 800 tall,
 * and walking down from the cap one pixel at a time is several hundred full re-wraps of the
 * message on every keystroke, where this is about nine.
 *
 * `measureAt` is handed the font and the candidate size for every measurement, because a canvas
 * only measures in the font currently set on it. The caller sets `ctx.font` from them and
 * returns `measureText`.
 */
export const fitTextToBox = (
    measureAt: (font: FontStyle, size: number, text: string) => number,
    font: FontStyle,
    text: string,
    box: TextBox,
    maxFontSize: number,
    minFontSize: number = MIN_FONT_SIZE
): FittedText => {
    const layoutAt = (size: number): { lines: string[]; fits: boolean } => {
        const lines = wrapText(
            (candidate) => measureAt(font, size, candidate),
            text,
            box.width
        );
        // `lines.length <= box.height / size`, written as the product so the half-pixel case is
        // decided once and consistently. Both operands are integers, so this is not rounding
        // either way: a line is drawn if it fits, and the last one is the one at risk.
        return { lines, fits: lines.length * size <= box.height };
    };

    // A cap below the floor is clamped up rather than trusted. The floor is the smallest this is
    // willing to draw, whatever the caller asks for.
    const ceiling = Math.max(minFontSize, Math.round(maxFontSize));

    const atCeiling = layoutAt(ceiling);
    if (atCeiling.fits) {
        return { lines: atCeiling.lines, fontSize: ceiling, truncated: false };
    }

    // The floor is the one size known to be worth asking about. If it does not fit either then no
    // size will, and the answer is truncation rather than a size.
    const atFloor = layoutAt(minFontSize);
    if (!atFloor.fits) {
        // Draw the floor's layout, cropped to what the box can hold, and report the truncation
        // -- rather than returning a size that overflows the card, which is the bug this replaces.
        const visible = atFloor.lines.slice(
            0,
            Math.max(1, Math.floor(box.height / minFontSize))
        );
        return {
            lines: visible,
            fontSize: minFontSize,
            truncated: visible.length < atFloor.lines.length,
        };
    }

    // `low` is known to fit and `high` is known not to, so the largest fitting size is inside the
    // interval and every step keeps that true.
    let low = minFontSize;
    let high = ceiling - 1;
    while (low < high) {
        const middle = Math.ceil((low + high) / 2);
        if (layoutAt(middle).fits) {
            low = middle;
        } else {
            high = middle - 1;
        }
    }

    return { lines: layoutAt(low).lines, fontSize: low, truncated: false };
};

/**
 * How many characters this box can hold at the smallest size it is willing to draw.
 *
 * This is the number that decides the character limit, and it is derived from the template
 * rather than the other way round. A fixed server-side cap is necessary -- something has to
 * refuse a note that is too long -- but it cannot be the number the sender sees, because the
 * templates do not have equal room. A card with a short body box holds fewer characters than one
 * with a tall one, and telling the sender they may write 1000 when their chosen card will only
 * show 700 means the last 300 are silently discarded at render time. So the box is the
 * authority and the cap is the ceiling: the effective limit is the smaller of the two.
 *
 * Measured at `minFontSize`, which is where the answer is worst-case. A message that fits here
 * fits at every larger size, so this is the true capacity of the card rather than its capacity
 * at the template's declared size -- and the fit will then be free to render that message
 * larger than the floor, which is the point of fitting at all.
 *
 * Binary search because capacity is close to linear in the text length and a box can hold
 * hundreds of characters: doubling until it overflows would cost `log2` full layouts, and this
 * is computed on every keystroke. Capacity is monotonic in length -- adding characters can
 * only add lines, never remove them -- so the search is well defined.
 *
 * The answer is a property of the *characters*, not of the character count: a run of CJK or
 * emoji occupies a full em where Latin occupies about half, so a card can hold fewer of them.
 * That is the honest reading of "how many characters fit" and it is why the caller passes the
 * text it is actually measuring rather than a fixed sample.
 */
export const maxFittingCharacters = (
    measureAt: (font: FontStyle, size: number, text: string) => number,
    font: FontStyle,
    text: string,
    box: TextBox,
    maxCharacters: number,
    minFontSize: number = MIN_FONT_SIZE
): number => {
    // The search measures prefixes, so it needs a string that is actually `maxCharacters` long.
    // Left to the caller that is a trap: pass a five-character sample and every prefix it can
    // form is at most five characters, so nothing ever overflows and the answer comes back as
    // the ceiling -- the number that is wrong in the direction that matters, because the caller
    // uses it to decide how much of a typed message to keep. Repeating the caller's own text
    // preserves the glyph mix, which is the thing the caller was measuring for.
    const characters = [...text];
    if (characters.length === 0) characters.push('x');
    while (characters.length < maxCharacters) {
        characters.push(...characters.slice(0, maxCharacters - characters.length));
    }

    const fits = (count: number): boolean => {
        const candidate = characters.slice(0, count).join('');
        const lines = wrapText(
            (piece) => measureAt(font, minFontSize, piece),
            candidate,
            box.width
        );
        return lines.length * minFontSize <= box.height;
    };

    if (fits(maxCharacters)) return maxCharacters;

    // `high` is known not to fit and `low` is known to fit, so the invariant holds at every
    // step and the loop terminates on the real capacity rather than on a guess.
    let low = 0;
    let high = maxCharacters;
    while (low < high) {
        const middle = Math.ceil((low + high) / 2);
        if (fits(middle)) {
            low = middle;
        } else {
            high = middle - 1;
        }
    }
    return low;
};