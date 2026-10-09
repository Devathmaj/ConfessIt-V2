import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AuthContext, type User } from '@/contexts/AuthContext';
import {
    fitTextToBox,
    fontForPreset,
    fontString,
    maxFittingCharacters,
    MAX_FONT_SIZE,
    wrapText,
    STYLE_PRESETS,
    MIN_FONT_SIZE,
    type FontStyle,
    type StylePreset,
    type TextBox,
} from '@/lib/loveNoteText';

// The page is stubbed at its API boundary rather than at the module boundary, because the
// canvas it draws on is the thing under test and the assertions have to reach the real one.
vi.mock('@/services/api', () => ({
    getLoveNoteRecipients: vi.fn().mockResolvedValue({ users: [], total: 0, skip: 0, limit: 20 }),
    getCohorts: vi.fn().mockResolvedValue([]),
    getReceivedLoveNotes: vi.fn().mockResolvedValue([]),
    sendLoveNote: vi.fn(),
}));

const { getLoveNoteRecipients, getCohorts, getReceivedLoveNotes } =
    await import('@/services/api');

/**
 * Fitting a love note's message into the card.
 *
 * The defect this covers is a *silent* one. The old renderer wrapped the message at the size
 * the template declared and, if the result was taller than the box, sliced off everything past
 * the last whole line and appended an ellipsis. Nothing told the sender. The note that arrived
 * was shorter than the note that was written, and the only visible symptom was a love note
 * that stopped mid-sentence -- which reads as the feature being broken rather than as a
 * message that was cut.
 *
 * So the assertions here are all one question asked different ways: does every character the
 * sender typed reach the card? That is checked against the *real* template rectangles rather
 * than a synthetic box, because the rectangles are the constraint. Template 3's body field is
 * 895x328 -- by far the tightest of the six, and the one a generic box would have got wrong.
 *
 * Text measurement is injected rather than taken from a canvas, because jsdom has no text
 * metrics: `measureText` on a jsdom context returns 0 for everything, so a test written against
 * a real context would conclude that every message fits and pass for the wrong reason. The
 * fake below models the only property that matters and is honest about it -- glyphs are
 * proportional to the font size, with a wide glyph for the scripts and emoji that are actually
 * in these notes.
 */

const SANS: FontStyle = { style: 'normal', weight: 'normal', family: 'sans-serif' };
const BOLD: FontStyle = { style: 'normal', weight: 'bold', family: 'sans-serif' };

/** Advance width per character at 1px, chosen so CJK and emoji are wider than Latin text. */
const advanceAt1px = (character: string): number => {
    const code = character.codePointAt(0) ?? 32;
    // A CJK ideograph or a kana is a full em; so is most of what people put on a love note.
    if (code >= 0x3000 && code <= 0x9fff) return 1;
    if (code >= 0x1f300) return 1;
    // Latin lowercase is roughly a half em, capitals a bit more, space a third.
    if (character === ' ') return 0.33;
    if (character >= 'a' && character <= 'z') return 0.52;
    if (character >= 'A' && character <= 'Z') return 0.62;
    return 0.55;
};

/** A canvas whose `measureText` is honest about the font it was last given. */
const makeContext = () => {
    let currentSize = 16;
    return {
        set size(size: number) {
            currentSize = size;
        },
        measure(text: string): number {
            let width = 0;
            // By code point, so an astral emoji is measured once at full width rather than as
            // two half-surrogates -- the same rule the renderer breaks lines on.
            for (const character of text) width += advanceAt1px(character) * currentSize;
            return width;
        },
    };
};

type FakeContext = ReturnType<typeof makeContext>;

/** `measureAt` in the shape `fitTextToBox` expects, bound to one context. */
const measureAtFor = (ctx: FakeContext) => (
    _font: FontStyle,
    size: number,
    text: string
): number => {
    ctx.size = size;
    return ctx.measure(text);
};

/**
 * A message of exactly `length` characters, counting the way the server counts.
 *
 * Built to a character count rather than to a sentence count on purpose. The limit under test
 * is 1000 *characters*, so a fixture made of 1000 sentences is ~19,000 characters and asserts
 * something much stronger than the feature claims -- a test that passes for the wrong reason is
 * the failure mode this file exists to avoid.
 */
const messageOfLength = (length: number): string => {
    const words = 'the quick brown fox jumps over a lazy dog today '.repeat(
        Math.ceil(length / 44) + 1
    );
    return [...words].slice(0, length).join('');
};

describe('how many characters a card can actually show', () => {
    // `maxFittingCharacters` exists because the rectangle is the real limit. The server allows
    // 1000 characters; a given card may not be able to show 1000 at a legible size, and the
    // honest limit is whichever runs out first. The reported number decides how much of a typed
    // message is kept, so it has to be the true one -- and an over-report is the dangerous
    // direction, because the sender keeps characters the renderer then has to drop.

    const capacityFor = (
        font: FontStyle,
        box: TextBox,
        sample: string,
        ceiling = 1000
    ) => maxFittingCharacters(measureAtFor(makeContext()), font, sample, box, ceiling);

    const fitsAtFloor = (box: TextBox, sample: string, count: number) =>
        fitTextToBox(
            measureAtFor(makeContext()),
            SANS,
            sample.repeat(count),
            box,
            MIN_FONT_SIZE
        ).truncated;

    it('never reports more than the card really fits', () => {
        // Template 3's body, asked for far more characters than it can hold at the floor, so the
        // search actually has to search. Using the production ceiling of 1000 would not test
        // anything here: every one of the six cards can show 1000 at 8px, so the ceiling always
        // binds and the result is the ceiling by definition.
        const box = TEMPLATE_3_BODY;
        const wide = '愛';
        const limit = capacityFor(SANS, box, wide, 6000);

        // Exactly the reported number must fit, and one more must not. An off-by-one in either
        // direction here is a character the sender is allowed to type and then loses.
        expect(limit).toBeLessThan(6000);
        expect(fitsAtFloor(box, wide, limit)).toBe(false);
        expect(fitsAtFloor(box, wide, limit + 1)).toBe(true);
    });

    it('holds fewer wide glyphs than narrow ones in the same space', () => {
        // The whole reason the limit is derived from the message rather than stored as a constant
        // per template: capacity is a property of the characters, not just the card.
        expect(capacityFor(SANS, TEMPLATE_3_BODY, '愛', 6000)).toBeLessThan(
            capacityFor(SANS, TEMPLATE_3_BODY, 'x', 6000)
        );
    });

    it('reports the same capacity in bold as in regular for this fake context', () => {
        // The fake does not vary glyph widths by weight, so this asserts the search is driven by
        // the font it is handed rather than by a measurement cached from some other call.
        expect(capacityFor(BOLD, TEMPLATE_3_BODY, 'x', 6000)).toBe(
            capacityFor(SANS, TEMPLATE_3_BODY, 'x', 6000)
        );
    });

    it('leaves the sender limit at the server cap for every real card', () => {
        // The honest finding: with the boxes the six templates actually declare, none of them is
        // the binding constraint at 1000 characters -- even a page of CJK fits at the floor size.
        // So the effective limit is the server's 1000 today, and the per-card capacity only
        // takes over if a card is tightened or the server cap is raised. This is here so that
        // the day that stops being true, it fails loudly here rather than silently discarding
        // senders' text.
        const boxes: Record<string, TextBox> = {
            'template 1': { x: 0, y: 0, width: 1150, height: 700 },
            'template 2': { x: 0, y: 0, width: 1570, height: 570 },
            'template 3': TEMPLATE_3_BODY,
            'template 4': { x: 0, y: 0, width: 990, height: 420 },
            'template 5': { x: 0, y: 0, width: 830, height: 800 },
            'template 6': { x: 0, y: 0, width: 1250, height: 600 },
        };
        for (const [name, box] of Object.entries(boxes)) {
            expect(capacityFor(SANS, box, 'x'), name).toBe(1000);
            expect(capacityFor(SANS, box, '愛', 1000), name).toBe(1000);
            expect(capacityFor(SANS, box, '🔥', 1000), name).toBe(1000);
        }
    });

    it('survives a sample shorter than the ceiling', () => {
        // The search measures prefixes, so a sample that is shorter than the ceiling would
        // otherwise be unable to overflow, and the answer would come back as the ceiling -- the
        // wrong answer, in the direction that lets the sender type more than the card can show.
        const box = TEMPLATE_3_BODY;
        expect(capacityFor(SANS, box, 'x', 6000)).toBe(
            capacityFor(SANS, box, 'x'.repeat(6000), 6000)
        );
        expect(capacityFor(SANS, box, '', 6000)).toBe(
            capacityFor(SANS, box, 'x', 6000)
        );
    });
});

/**
 * The draw pass has to agree with the fit, and it has to paint what the sender chose.
 *
 * The size bug this pins is a real one that shipped in this branch and was caught by looking at
 * the output rather than by any test: the fit correctly returned a smaller `fontSize`, and the
 * renderer took only its `lines` and then drew them at the *template's* size. The wrap had been
 * computed against one font and the glyphs painted in another, so the lines ran past the box --
 * the exact failure the feature exists to remove, reintroduced by the feature.
 *
 * The style and colour tests below pin the same class of defect one layer out. A picker that
 * changes state but never reaches `ctx.font` / `ctx.fillStyle` looks correct in the UI and
 * produces a card that ignores the sender, which is indistinguishable from the picker not
 * working at all.
 *
 * These drive the real renderer from `LoveNotesPage.tsx` and inspect the calls `fillText`
 * received, which is the only place the two can be caught disagreeing: `fitTextToBox` on its own
 * is correct and would pass. Reverting the draw to `field.font.size` makes the size assertion
 * fail, which is what establishes the test is load-bearing.
 */
describe('the renderer paints what the fit and the sender asked for', () => {
    /** The px value out of a `ctx.font` string, which is how the renderer states the size. */
    const sizeIn = (entry: { font: string }): number =>
        parseFloat(/(\d+(?:\.\d+)?)px/.exec(entry.font)?.[1] ?? '0');
    // The page reads the account through `useAuth` for the signature field, so a provider is
    // needed to mount it at all.
    const authValue = {
        user: { id: 'me', name: 'Me', regno: 'CS-9' } as User,
        logout: vi.fn(),
        isAuthenticated: true,
        isLoading: false,
        token: 't' as string | null,
        verifyMagicToken: vi.fn(),
        fetchAndSetUser: vi.fn(),
        can: () => false,
        signInPath: '/login',
    };

    const loadPage = async () => {
        const page = await import('@/pages/LoveNotesPage');
        return page;
    };

    const stubCanvas = () => {
        const drawn: { text: string; font: string; fillStyle: string; x: number; y: number }[] = [];
        const context = {
            font: '10px sans-serif',
            fillStyle: '#000',
            textAlign: 'center',
            textBaseline: 'middle',
            measureText: (text: string) => {
                // Same model as `makeContext`: proportional to the font currently set.
                const size = parseFloat(/(\d+(?:\.\d+)?)px/.exec(context.font)?.[1] ?? '10');
                let width = 0;
                for (const character of text) width += advanceAt1px(character) * size;
                return { width };
            },
            fillText: (text: string, x: number, y: number) => {
                // `fillStyle` is read at paint time, not at assignment time, because the renderer
                // sets it once per field and then draws many lines.
                drawn.push({
                    text,
                    font: context.font,
                    fillStyle: context.fillStyle,
                    x,
                    y,
                });
            },
            drawImage: () => {},
        };

        HTMLCanvasElement.prototype.getContext = vi.fn(() => context) as never;

        // The preview PNG has to "load" or the effect never draws anything. `width` and `height`
        // are all the renderer reads off it.
        vi.stubGlobal(
            'Image',
            class {
                onload: (() => void) | null = null;
                width = 1600;
                height = 900;
                set src(_value: string) {
                    // Assigning `src` is what the effect does; call back synchronously so the
                    // assertions below do not need to wait on a real network fetch.
                    queueMicrotask(() => this.onload?.());
                }
            }
        );

        return { drawn, context };
    };

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    /**
     * Mounts the real page over a single-field template whose body colour is `bodyColor`, and
     * returns the canvas stub so the test can read what was actually painted.
     */
    const mountPage = async (bodyColor = '#0078F0') => {
        const { LoveNotesPage } = await loadPage();
        const canvas = stubCanvas();

        const template = {
            template_name: 'test',
            image: 'template_3.png',
            fields: [
                {
                    name: 'body',
                    type: 'text',
                    x: TEMPLATE_3_BODY.x,
                    y: TEMPLATE_3_BODY.y,
                    width: TEMPLATE_3_BODY.width,
                    height: TEMPLATE_3_BODY.height,
                    font: { size: 32, color: bodyColor },
                },
            ],
        };

        vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
            // The page fetches all six template JSONs in parallel and keys the picker buttons by
            // `template_name`, so answering every request with the same object would render six
            // identically-named buttons and trip React's duplicate-key warning. The warning is the
            // kind of thing a test should not add to the output, because it teaches you to ignore
            // it in the run where it is real. Naming them by the URL the page asked for keeps the
            // stub honest about the only thing it is faking.
            const which = /template_(\d)\.json/.exec(String(input))?.[1] ?? '1';
            return { json: async () => ({ ...template, template_name: `template ${which}` }) } as never;
        });
        vi.mocked(getLoveNoteRecipients).mockResolvedValue({ users: [], total: 0, skip: 0, limit: 20 });
        vi.mocked(getCohorts).mockResolvedValue([]);
        vi.mocked(getReceivedLoveNotes).mockResolvedValue({ notes: [], total: 0, skip: 0, limit: 0 });

        render(
            <AuthContext.Provider value={authValue}>
                <MemoryRouter>
                    <LoveNotesPage />
                </MemoryRouter>
            </AuthContext.Provider>
        );

        return { ...canvas, textarea: await screen.findByRole('textbox') };
    };

    it('never paints a line outside the box it was fitted to', async () => {
        const { drawn, context, textarea } = await mountPage();
        const bodyField = TEMPLATE_3_BODY;

        // The placeholder is the short-note case, and it is what proves the ceiling end of the
        // behaviour through the real page rather than only through the pure function.
        await waitFor(() => expect(drawn.length).toBeGreaterThan(0));
        const placeholderDraws = drawn.slice();
        const placeholderSize = sizeIn(placeholderDraws[placeholderDraws.length - 1]);
        const beforeMessage = drawn.length;

        // Long enough that the renderer has to shrink well below the placeholder.
        fireEvent.change(textarea, { target: { value: messageOfLength(1000) } });

        await waitFor(() => expect(drawn.length).toBeGreaterThan(beforeMessage));

        const bodyDraws = drawn.slice(beforeMessage);
        expect(bodyDraws.length).toBeGreaterThan(1);

        // The size actually painted must be the shrunk one, and it must be the same for every
        // line -- one size per field, not the cap the text was wrapped against.
        const paintedSizes = new Set(bodyDraws.map(sizeIn));
        expect([...paintedSizes]).toHaveLength(1);
        const size = [...paintedSizes][0];

        // This is the dynamic behaviour, asserted end to end: a short note is drawn at the
        // moderate ceiling and a full one smaller, through the real canvas effect. When the page
        // was still capping at the declared 32px, `placeholderSize` was 32 and the two were equal.
        expect(placeholderSize).toBe(MAX_FONT_SIZE);
        expect(size).toBeLessThan(placeholderSize);
        expect(placeholderSize / size).toBeGreaterThan(1.5);

        // And every line must land inside the vertical bounds of the box. This is the assertion
        // that would have caught the draw-at-the-wrong-size bug: the lines were correct but drawn
        // apart from centres computed for a smaller size, so the block overflowed top and bottom.
        const top = Math.min(...bodyDraws.map((entry) => entry.y));
        const bottom = Math.max(...bodyDraws.map((entry) => entry.y));
        expect(top).toBeGreaterThanOrEqual(bodyField.y);
        expect(bottom).toBeLessThanOrEqual(bodyField.y + bodyField.height);

        // Every glyph is horizontally inside the box too, at the size actually used.
        context.font = `${size}px sans-serif`;
        for (const entry of bodyDraws) {
            expect(context.measureText(entry.text).width).toBeLessThanOrEqual(bodyField.width);
        }
        // The default 5s is not enough for this one: it mounts the whole page, then makes the
        // real fitting path run twice over a 1000-character message -- once to draw and once for
        // the per-card capacity search -- and each of those re-wraps the message several times.
        // Alone it takes about two seconds; in a parallel run it has been measured past six. The
        // budget is the cost of the thing being tested, not a flake being waited out.
    }, 30000);

    it('paints the message in the colour the card itself declares', async () => {
        // The starting state has to be the designer's colour, not a hard-coded one. Every template
        // declares its own, and template 5's is near-white on a dark card -- a page that opened on
        // black would make that card unreadable before the sender touched anything.
        const { drawn } = await mountPage('#F0F4FF');
        await waitFor(() => expect(drawn.length).toBeGreaterThan(0));

        expect(drawn.every((entry) => entry.fillStyle === '#F0F4FF')).toBe(true);
    });

    it('paints in the colour the sender picked, and lets them go back to the card', async () => {
        const { drawn } = await mountPage('#0078F0');
        await waitFor(() => expect(drawn.length).toBeGreaterThan(0));

        fireEvent.click(screen.getByRole('button', { name: 'Crimson' }));
        await waitFor(() => expect(drawn.at(-1)?.fillStyle).toBe('#C1121F'));

        // The escape hatch is not optional here: a sender who picked a colour that clashes with the
        // artwork has to be able to hand the design back, and "Card default" is what does that.
        fireEvent.click(screen.getByRole('button', { name: 'Card default' }));
        await waitFor(() => expect(drawn.at(-1)?.fillStyle).toBe('#0078F0'));
    });

    it.each([
        ['Normal', 'normal normal'],
        ['Bold', 'normal bold'],
        ['Italic', 'italic normal'],
        ['Bold Italic', 'italic bold'],
    ])('paints %s as "%s"', async (label, expected) => {
        // Asserted on the font shorthand, which is the only thing `ctx.font` ever is. `Normal` is
        // included even though it is the default and clicking it changes no state -- a preset that
        // silently painted bold would be a bug the other three cases would not catch.
        const { drawn } = await mountPage();
        await waitFor(() => expect(drawn.length).toBeGreaterThan(0));

        fireEvent.click(screen.getByRole('button', { name: label }));
        await waitFor(() => expect(drawn.at(-1)?.font.startsWith(expected)).toBe(true));
    });
});

/** The tightest body box of the six shipped templates, and the widest, both verbatim. */
const TEMPLATE_3_BODY: TextBox = { x: 636, y: 295, width: 895, height: 328 };
const TEMPLATE_1_BODY: TextBox = { x: 390, y: 180, width: 1150, height: 700 };
const TEMPLATE_5_BODY: TextBox = { x: 1025, y: 110, width: 830, height: 800 };

const sentences = (count: number): string =>
    Array.from({ length: count }, (_, i) => `line ${i} of the note`).join(' ');

/**
 * The characters of `message` as they land on the card, with whitespace collapsed away.
 *
 * Joining the lines and comparing to the input is the assertion that matters -- that nothing was
 * dropped -- but it cannot be a literal comparison. Wrapping consumes the space at a break, and
 * a break inside a long word joins two fragments that were never separated, so a line break can
 * add or remove exactly one space. Comparing with whitespace removed is what makes the check
 * about characters surviving rather than about where the lines happened to fall.
 */
const drawn = (lines: string[]): string => lines.join('').replace(/\s+/g, '');

describe('wrapText', () => {
    it('keeps a message that fits on one line as one line', () => {
        const ctx = makeContext();
        expect(wrapText((t) => ctx.measure(t), 'hello there', 10_000)).toEqual(['hello there']);
    });

    it('breaks on words so the result reads like prose', () => {
        const ctx = makeContext();
        const lines = wrapText((t) => ctx.measure(t), 'alpha beta gamma', 100);

        // Greedy packing, not one word per line: 'alpha beta' is narrower than the box at 16px,
        // so it is kept together and only 'gamma' wraps. Asserting the exact layout here is what
        // distinguishes word wrapping from a character-level split, which is the property that
        // makes a note readable rather than merely intact.
        expect(lines).toEqual(['alpha beta', 'gamma']);
        for (const line of lines) expect(ctx.measure(line)).toBeLessThanOrEqual(100);
    });

    it('keeps a blank line the sender typed', () => {
        const ctx = makeContext();
        // An empty paragraph used to vanish, taking the sender's paragraph break with it.
        expect(wrapText((t) => ctx.measure(t), 'one\n\ntwo', 10_000)).toEqual(['one', '', 'two']);
    });

    it('splits a single word that is wider than the box, rather than overflowing it', () => {
        const ctx = makeContext();
        ctx.size = 16;
        const lines = wrapText((t) => ctx.measure(t), 'x'.repeat(200), 100);

        expect(lines.length).toBeGreaterThan(1);
        for (const line of lines) expect(ctx.measure(line)).toBeLessThanOrEqual(100);
    });

    it('never splits an emoji into half a surrogate', () => {
        const ctx = makeContext();
        const emoji = '\u{1F495}';
        const lines = wrapText((t) => ctx.measure(t), emoji.repeat(30), 100);

        expect(lines.length).toBeGreaterThan(1);
        for (const line of lines) {
            // The check is that the line is a whole number of emoji, not that it contains no
            // surrogate units -- every emoji contains two. A line split between the halves would
            // iterate to a lone high surrogate, which `codePointAt` reports as itself.
            expect([...line].every((c) => c === emoji)).toBe(true);
            expect(line.length).toBe([...line].length * 2);
        }
        // And no character was lost doing it.
        expect(lines.join('')).toBe(emoji.repeat(30));
    });
});

describe('MAX_FONT_SIZE', () => {
    it('is a moderate ceiling, not a fraction of the card', () => {
        // The ceiling used to be two fifths of the box height, which on the tallest template came
        // to 320px -- a two-word note rendered as a billboard. It is one constant now, so the
        // bounds stay loose on purpose: the value is worth tuning, but it must remain
        // heading-sized -- clearly larger than the declared 32 the templates shipped, and
        // nowhere near a headline on the 1920x1080 card. It also has to sit on every body box
        // rather than dominate even the tightest one (template 3 is 328 tall).
        expect(MAX_FONT_SIZE).toBeGreaterThan(32);
        expect(MAX_FONT_SIZE).toBeLessThanOrEqual(64);
        for (const box of [TEMPLATE_1_BODY, TEMPLATE_3_BODY, TEMPLATE_5_BODY]) {
            expect(MAX_FONT_SIZE).toBeLessThanOrEqual(box.height / 2);
        }
    });

    it('draws a short note at the same size on every card', () => {
        // The complaint this answers: the ceiling tracked the box, so the same two words were
        // drawn at a different size on every template, largest on the tallest. "hi" fits every
        // box at the ceiling, so the ceiling is the size on all of them now.
        for (const box of [TEMPLATE_1_BODY, TEMPLATE_3_BODY, TEMPLATE_5_BODY]) {
            const fitted = fitTextToBox(
                measureAtFor(makeContext()),
                SANS,
                'hi',
                box,
                MAX_FONT_SIZE
            );
            expect(fitted.fontSize, `${box.width}x${box.height}`).toBe(MAX_FONT_SIZE);
        }
    });

    it('never draws below the floor, even under a cap that is', () => {
        // The old cap function clamped a below-the-floor request up rather than trusting it, and
        // `fitTextToBox` keeps that rule now that it owns the ceiling: a caller can impose a cap
        // but cannot talk the renderer below its floor.
        const fitted = fitTextToBox(
            measureAtFor(makeContext()),
            SANS,
            'hi',
            TEMPLATE_1_BODY,
            2
        );
        expect(fitted.fontSize).toBe(MIN_FONT_SIZE);
    });
});

describe('the ceiling is a moderate constant and the fit moves below it', () => {
    // Every shipped template declares `body.font.size: 32` against boxes from 830x800 to 1570x570
    // on a 1920x1080 card. A uniform 32 across those is a placeholder, not a design: it is 1.7% of
    // the card's width, which is why the message read as tiny. The ceiling is `MAX_FONT_SIZE`
    // instead -- one moderate number for every card -- and these are the sizes the page produces.

    const BOXES: [string, TextBox][] = [
        ['template 1', TEMPLATE_1_BODY],
        ['template 3', TEMPLATE_3_BODY],
        ['template 5', TEMPLATE_5_BODY],
    ];

    const sizeFor = (box: TextBox, length: number) =>
        fitTextToBox(
            measureAtFor(makeContext()),
            SANS,
            messageOfLength(length),
            box,
            MAX_FONT_SIZE
        ).fontSize;

    it.each(BOXES)('shrinks the type only once the message stops fitting on %s', (_name, box) => {
        const sizes = [10, 300, 600, 1000].map((length) => sizeFor(box, length));

        // Never larger than the ceiling, and never growing as the message grows: the fit only
        // ever moves down, and only because the box leaves it no choice.
        for (const size of sizes) expect(size).toBeLessThanOrEqual(MAX_FONT_SIZE);
        for (let i = 1; i < sizes.length; i += 1) {
            expect(sizes[i]).toBeLessThanOrEqual(sizes[i - 1]);
        }
        // A short message sits at the ceiling -- not resized for no reason...
        expect(sizes[0]).toBe(MAX_FONT_SIZE);
        // ...and a full-length one really has been brought down, so the shrink is real rather
        // than nominal.
        expect(sizes[sizes.length - 1]).toBeLessThan(MAX_FONT_SIZE);
    });

    it.each(BOXES)('keeps a full-length note inside the box on %s', (_name, box) => {
        // The ceiling change must not cost the original guarantee: at the server's cap, all
        // 1000 characters still reach the card at a legible size.
        const fitted = fitTextToBox(
            measureAtFor(makeContext()),
            SANS,
            messageOfLength(1000),
            box,
            MAX_FONT_SIZE
        );
        expect(fitted.truncated).toBe(false);
        expect(fitted.lines.length * fitted.fontSize).toBeLessThanOrEqual(box.height);
        expect(fitted.fontSize).toBeGreaterThanOrEqual(16);
    });

    it('never truncates at the cap on any shipped template', () => {
        // Template 5 is the narrow one -- 830 wide and 800 tall -- so it is where a search that
        // gives up early shows up. An earlier version of this walked down from the cap in
        // fractional steps, overshot the answer, and landed on the floor with the note truncated;
        // this is the assertion that says the search converges instead.
        const shipped: TextBox[] = [
            TEMPLATE_1_BODY,
            { x: 180, y: 273, width: 1570, height: 570 },
            TEMPLATE_3_BODY,
            { x: 455, y: 225, width: 990, height: 420 },
            TEMPLATE_5_BODY,
            { x: 330, y: 249, width: 1250, height: 600 },
        ];
        for (const box of shipped) {
            const fitted = fitTextToBox(
                measureAtFor(makeContext()),
                SANS,
                messageOfLength(1000),
                box,
                MAX_FONT_SIZE
            );
            expect(fitted.truncated, `${box.width}x${box.height}`).toBe(false);
            expect(fitted.fontSize, `${box.width}x${box.height}`).toBeGreaterThanOrEqual(16);
        }
    });

    it('finds the exact largest size, not merely one that fits', () => {
        // Binary search over a monotonic fit can land anywhere in the gap; this pins it to the
        // boundary, because "the largest that works" is the whole contract.
        const box = TEMPLATE_3_BODY;
        const size = sizeFor(box, 400);

        // Asking for exactly `size` returns that size; asking one higher has to come back smaller,
        // which is only true if `size` really was the last one that fitted.
        const requested = (cap: number) =>
            fitTextToBox(
                measureAtFor(makeContext()),
                SANS,
                messageOfLength(400),
                box,
                cap
            ).fontSize;

        expect(requested(size)).toBe(size);
        expect(requested(size + 1)).toBeLessThan(size + 1);
    });
});

describe('fontForPreset', () => {
    it('offers exactly the four combinations the sender asked for', () => {
        // Normal, Bold, Italic, Bold Italic -- and nothing else. This is the whole point of
        // modelling it as one four-way value: two independent bold/italic toggles also admit
        // "bold and normal", which draws the same pixels as bold while claiming otherwise.
        expect(STYLE_PRESETS.map((preset) => preset.value)).toEqual([
            'normal',
            'bold',
            'italic',
            'bold-italic',
        ]);
    });

    it('maps each preset to a distinct slant and weight', () => {
        // The four cases are meant to be four different renderings. If any two collapsed to the
        // same pair the picker would offer a button that changes nothing.
        const resolved = STYLE_PRESETS.map((preset) => fontForPreset(preset.value));
        expect(resolved).toEqual([
            { style: 'normal', weight: 'normal' },
            { style: 'normal', weight: 'bold' },
            { style: 'italic', weight: 'normal' },
            { style: 'italic', weight: 'bold' },
        ]);
        expect(new Set(resolved.map((entry) => JSON.stringify(entry))).size).toBe(4);
    });

    it('cannot be asked for bold and normal at once', () => {
        // `normal` is a value in its own right, so it is unreachable alongside anything else rather
        // than merely discouraged -- there is no second boolean to set.
        expect(fontForPreset('normal').weight).toBe('normal');
        expect(fontForPreset('normal').style).toBe('normal');
    });

    it('falls back to normal for a value it does not recognise', () => {
        // A `StylePreset` arriving from a stale session or a hand-edited payload must not throw or
        // produce an undefined slant that `fontString` would splice into the CSS as "undefined".
        const resolved = fontForPreset('nonsense' as StylePreset);
        expect(resolved).toEqual({ style: 'normal', weight: 'normal' });
    });

    it('builds a CSS font shorthand that names both slant and weight', () => {
        // The reason bold *and* italic was not expressible before is that this string is what
        // reaches `ctx.font`, and it takes both properties independently.
        expect(fontString({ ...fontForPreset('bold-italic'), family: 'serif' }, 20)).toBe(
            'italic bold 20px serif'
        );
    });
});

describe('fitTextToBox', () => {
    it('honours a cap the caller imposes, using it whole for a short message', () => {
        // The cap is a real input, not an internal detail: the page passes `MAX_FONT_SIZE`, but
        // a caller with a different idea can impose one. A short message fits under the cap, so
        // the cap is the size -- the fit takes the largest that works, and that is the cap.
        const ctx = makeContext();
        const fitted = fitTextToBox(
            measureAtFor(ctx),
            SANS,
            'thinking of you today',
            TEMPLATE_1_BODY,
            32
        );

        expect(fitted.fontSize).toBe(32);
        expect(fitted.truncated).toBe(false);
        expect(fitted.lines).toEqual(['thinking of you today']);
    });

    it('scales the type down as the message grows', () => {
        const ctx = makeContext();
        const sizes = [10, 25, 50, 100, 250, 500, 1000].map(
            (words) =>
                fitTextToBox(measureAtFor(ctx), SANS, sentences(words), TEMPLATE_3_BODY, 32)
                    .fontSize
        );

        // Strictly decreasing is the property that matters: the whole point is that a longer
        // note is a smaller note, not a clipped one.
        for (let i = 1; i < sizes.length; i++) {
            expect(sizes[i]).toBeLessThanOrEqual(sizes[i - 1]);
        }
        expect(sizes[sizes.length - 1]).toBeLessThan(sizes[0]);
    });

    it.each([
        ['the tightest template', TEMPLATE_3_BODY],
        ['a roomy template', TEMPLATE_1_BODY],
        ['a tall narrow one', TEMPLATE_5_BODY],
    ])('shows every character of a 1000-character note on %s', (_label, box) => {
        const message = messageOfLength(1000);
        const ctx = makeContext();
        const fitted = fitTextToBox(measureAtFor(ctx), SANS, message, box, 32);

        expect(fitted.truncated).toBe(false);
        // The whole point: nothing is dropped and nothing is ellipsised.
        expect(drawn(fitted.lines)).toBe(message.replace(/\s+/g, ''));
        // And it is still drawn inside the box rather than off the bottom of the card.
        expect(fitted.lines.length * fitted.fontSize).toBeLessThanOrEqual(box.height);
    });

    it('keeps the note legible at the character limit on the tightest template', () => {
        const ctx = makeContext();
        const fitted = fitTextToBox(
            measureAtFor(ctx),
            SANS,
            messageOfLength(1000),
            TEMPLATE_3_BODY,
            32
        );

        // This is why LOVE_NOTE_BODY_MAX is 1000 and not 2000. The floor exists so the search
        // terminates; the server cap is what keeps real notes well above it, and this is the
        // assertion that says the two numbers still agree after either one moves.
        expect(fitted.fontSize).toBeGreaterThanOrEqual(16);
    });

    it('never returns a size below the floor', () => {
        const ctx = makeContext();
        const fitted = fitTextToBox(measureAtFor(ctx), SANS, sentences(2000), TEMPLATE_3_BODY, 32);

        expect(fitted.fontSize).toBeGreaterThanOrEqual(MIN_FONT_SIZE);
    });

    it('clamps a template whose declared size is already below the floor', () => {
        // Rather than trusting the JSON to ask for something drawable: a template that declared
        // 4px would otherwise be honoured and produce a note nobody can read.
        const ctx = makeContext();
        const fitted = fitTextToBox(measureAtFor(ctx), SANS, 'hi', TEMPLATE_1_BODY, 4);

        expect(fitted.fontSize).toBe(MIN_FONT_SIZE);
    });

    it('reports truncation instead of returning a size that overflows the box', () => {
        // A box with no room at all. The old renderer would have drawn the message at 32px over
        // the whole card; the contract now is that it never claims to have fitted something.
        const ctx = makeContext();
        const fitted = fitTextToBox(
            measureAtFor(ctx),
            SANS,
            sentences(400),
            { x: 0, y: 0, width: 895, height: 8 },
            32
        );

        expect(fitted.truncated).toBe(true);
        expect(fitted.fontSize).toBe(MIN_FONT_SIZE);
        expect(fitted.lines).toHaveLength(1);
    });

    it('fits CJK, emoji and Devanagari at the limit without dropping any of them', () => {
        // The scripts these notes are actually written in. All wider than Latin, so they are the
        // case a Latin-only fit would fail -- and the character-level splitter is what has to
        // carry a run of emoji, since there is no space to break on.
        const message = [
            'そばにいてください',
            '\u{1F495}\u{1F496}\u{1F497}',
            'मेरे है तुम',
            '\u{1F468}‍\u{1F469}‍\u{1F467}',
        ].join(' ').repeat(30);

        const ctx = makeContext();
        const fitted = fitTextToBox(measureAtFor(ctx), SANS, message, TEMPLATE_3_BODY, 32);

        expect(fitted.truncated).toBe(false);
        expect([...drawn(fitted.lines)]).toEqual([...message.replace(/\s+/g, '')]);
        for (const line of fitted.lines) {
            expect(ctx.measure(line)).toBeLessThanOrEqual(TEMPLATE_3_BODY.width);
        }
    });

    it('fits the same message in bold as it does in regular', () => {
        // The weight reaches the canvas through the same `fontString`, so this guards the path
        // where a style is applied to `fillText` but not to the measuring pass -- a mismatch
        // there measures the wrong glyphs and fits text that does not fit.
        const ctx = makeContext();
        const message = sentences(400);

        const regular = fitTextToBox(measureAtFor(ctx), SANS, message, TEMPLATE_3_BODY, 32);
        const bold = fitTextToBox(measureAtFor(ctx), BOLD, message, TEMPLATE_3_BODY, 32);

        expect(bold.truncated).toBe(false);
        expect(drawn(bold.lines)).toBe(message.replace(/\s+/g, ''));
        expect(regular.truncated).toBe(false);
    });

    it('preserves the paragraph breaks in a multi-paragraph note', () => {
        const ctx = makeContext();
        const message = 'first line\n\nsecond line';
        const fitted = fitTextToBox(measureAtFor(ctx), SANS, message, TEMPLATE_3_BODY, 32);

        expect(fitted.lines).toEqual(['first line', '', 'second line']);
    });
});