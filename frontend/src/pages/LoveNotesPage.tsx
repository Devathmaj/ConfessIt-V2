// src/pages/LoveNotesPage.tsx

import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useAuth, User } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { CountdownTimer } from '@/components/ui/countdown-timer';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { FloralDecor } from '@/components/FloralDecor';
import { ScatteredIcons } from '@/components/ScatteredIcons';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Badge } from '@/components/ui/badge';
import {
    AlertDialog,
    AlertDialogAction,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {
    Mail,
    Palette,
    Send,
    Download,
    X,
    MessageCircle,
    Users,
    Search,
    Info,
    Flag,
    ChevronDown,
    Loader2
} from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';
import { ProfileAvatar } from '@/components/ProfileAvatar';
import { ReportLoveNoteDialog } from '@/components/ReportLoveNoteDialog';
import { getLoveNoteRecipients, getCohorts, sendLoveNote, getReceivedLoveNotes, markLoveNoteRead } from '@/services/api';
import type { Cohort } from '@/lib/accountFields';
import {
  ALL_COHORTS,
  cohortFromKey,
  cohortKey,
  cohortLabel,
} from '@/lib/accountFields';
import { formatDateTimeDDMMYYYY } from '@/lib/utils';
import {
    fitTextToBox,
    fontString,
    fontForPreset,
    maxFittingCharacters,
    MAX_FONT_SIZE,
    wrapText,
    STYLE_PRESETS,
    type FontStyle,
    type StylePreset,
} from '@/lib/loveNoteText';

// Used to define the structure for a template's field
interface TemplateField {
    name: string;
    type: 'text' | 'static_text' | 'emoji';
    value?: string;
    x: number;
    y: number;
    width: number;
    height: number;
    font?: {
        size: number;
        color: string;
    };
}

// Used to define the structure for a template
interface Template {
    template_name: string;
    image: string;
    fields: TemplateField[];
}

// Used to define the structure for a received love note
interface LoveNote {
    id: string;
    from: string;
    body: string;
    image_url: string;
    anonymous: boolean;
    created_at: string | null;
    read_at: string | null;
}

// Used to define the structure for a user in the recipient list
interface Recipient extends User {
    id: string;
}

const emojiStickers = ['💕', '💖', '💗', '💘', '💝', '💌', '🌹', '🌸', '🌺', '🦋', '✨', '💫', '⭐', '🌟', '💎', '👑'];
const availableFonts = ['sans-serif', 'serif', 'monospace', 'cursive'];

/**
 * Basic colours for the message, rather than a free colour picker.
 *
 * A picker would let the sender put white on template 1 and dark navy on template 5, both of which
 * are unreadable, and neither card can be told apart from the other while choosing. A fixed palette
 * makes every combination at least legible on every card, which is the constraint that actually
 * matters here.
 */
const noteTextColors = [
    { value: '#000000', label: 'Black' },
    { value: '#FFFFFF', label: 'White' },
    { value: '#C1121F', label: 'Crimson' },
    { value: '#E75480', label: 'Rose' },
    { value: '#6A0572', label: 'Purple' },
    { value: '#1B3A6B', label: 'Navy' },
    { value: '#2D6A4F', label: 'Green' },
    { value: '#8B5E3C', label: 'Brown' },
    { value: '#6C757D', label: 'Grey' },
];

// Keep in step with the backend's MAX_IMAGE_SIZE_MB (2MB) cap on the decoded love note
// image, so an oversized note is refused before it is built into a request body.
const MAX_NOTE_IMAGE_BYTES = 2 * 1024 * 1024;

// The ceiling on a love note's message. Mirrors LOVE_NOTE_BODY_MAX in backend/app/limits.py,
// which is where it is actually enforced -- this copy exists so the sender never gets further
// than the server allows.
//
// It is a *ceiling*, not the number the sender sees. The template's `body` box is the
// authority: a card with a short box holds fewer characters than one with a tall box, and the
// sender's chosen card is what their note will be drawn on. The effective limit is whichever is
// smaller, computed from the real box in `effectiveBodyLimit` below. At 2000 -- what this was
// before -- the binding limit was always the 1000 here and never the card, which is why the
// renderer had to cut text off: the sender was allowed to write more than their card could show.
//
// A character here is a Unicode code point, not a UTF-16 unit, so it is counted rather than
// taken from `.length`. An emoji outside the BMP is one character the sender means to write
// and two units, and counting units would spend a third of the allowance on half as much prose.
// `maxLength` on the textarea cannot express this, so it is left off and the count is enforced
// on the way in instead.
const LOVE_NOTE_BODY_MAX = 1000;

/** Code points in `value`, which is what the server's cap counts. */
const characterCount = (value: string): number => [...value].length;

/**
 * How many notes one inbox read returns. Cards carry images, so twelve keeps one read
 * cheap while a "show more" click still grows the grid by visible rows. The backend
 * clamps anything larger regardless.
 */
const INBOX_PAGE_SIZE = 12;

/**
 * How many recipients one page of the picker holds. Small enough that opening the send
 * dialog never reads the participant table, large enough that the four-visible-rows list
 * can be scrolled a few times before a "load more" click. The backend clamps anything
 * larger regardless.
 */
const RECIPIENT_PAGE_SIZE = 20;

/** The rows inside an inbox answer, whether it is the page envelope or a legacy bare array. */
const inboxRowsOf = (answer: unknown): LoveNote[] => {
  if (Array.isArray(answer)) return answer as LoveNote[];
  const notes = (answer as { notes?: LoveNote[] } | null | undefined)?.notes;
  return Array.isArray(notes) ? notes : [];
};

/** The matching-row count beside an inbox answer, falling back to the rows at hand. */
const inboxTotalOf = (answer: unknown, fallback: number): number => {
  if (Array.isArray(answer)) return (answer as unknown[]).length;
  const total = (answer as { total?: unknown } | null | undefined)?.total;
  return typeof total === 'number' && Number.isFinite(total) ? total : fallback;
};

export const LoveNotesPage = () => {
const { user, fetchAndSetUser, token } = useAuth();
    const [searchParams, setSearchParams] = useSearchParams();
    const [activeTab, setActiveTab] = useState<'create' | 'inbox'>('create');
    const [templates, setTemplates] = useState<Template[]>([]);
    const [selectedTemplate, setSelectedTemplate] = useState<Template | null>(null);
    const [noteMessage, setNoteMessage] = useState('');
    const [isAnonymous, setIsAnonymous] = useState(false);
    const [selectedEmoji, setSelectedEmoji] = useState<string | null>(null);
    const [selectedFont, setSelectedFont] = useState<string>('sans-serif');
    const [selectedStylePreset, setSelectedStylePreset] = useState<StylePreset>('normal');
    // `null` means "whatever the card itself wants", which is not the same as black: template 5's
    // body is declared `#F0F4FF` because the card is dark, so defaulting to black would make a
    // perfectly good template unreadable. Holding the override as null rather than seeding it with
    // the first template's colour is what lets it follow the card when the sender switches.
    const [colorOverride, setColorOverride] = useState<string | null>(null);
const [selectedNote, setSelectedNote] = useState<LoveNote | null>(null);
    /**
     * The inbox on screen: always the contiguous window [0, length) of the
     * newest-first server order, so a refresh is one clean re-read and a "show more"
     * click appends beneath the notes already read.
     */
    const [receivedNotes, setReceivedNotes] = useState<LoveNote[]>([]);
    /** How many notes the inbox holds, loaded or not. */
    const [inboxTotal, setInboxTotal] = useState(0);
    /** False while answers arrive as legacy bare arrays (older servers, unit-test mocks). */
    const [pagedInbox, setPagedInbox] = useState(false);
    const [loadingNotes, setLoadingNotes] = useState(false);
    const [loadingMoreNotes, setLoadingMoreNotes] = useState(false);
    const [viewingImageUrl, setViewingImageUrl] = useState<string | null>(null);
    const canvasRef = useRef<HTMLCanvasElement>(null);

    // A second context used only for measuring, so the character limit can be derived from the
    // template's box without waiting for the preview PNG to load.
    //
    // It cannot be the visible canvas: measuring has to happen during render, to decide what
    // the sender is allowed to type, while the visible canvas is only usable inside `img.onload`
    // -- a callback that has not run yet on first paint. A detached 1x1 canvas gives a real
    // `measureText` immediately and costs nothing; the context holds no pixels.
    const measureCanvasRef = useRef<HTMLCanvasElement | null>(null);
    if (measureCanvasRef.current === null && typeof document !== 'undefined') {
        measureCanvasRef.current = document.createElement('canvas');
    }

    // Which notes this account has already reported. The server refuses a second report, so
    // the button is hidden rather than left to fail: a control that can only ever answer
    // "already reported" is worse than no control. The inbox payload does not carry report
    // state, so this is the page's own record of it and it resets with the page.
    const [reportedNoteIds, setReportedNoteIds] = useState<string[]>([]);
    const [noteToReport, setNoteToReport] = useState<LoveNote | null>(null);


    // State for recipient selection modal
    const [isRecipientModalOpen, setIsRecipientModalOpen] = useState(false);
    /**
     * The recipient window on screen: the contiguous [0, length) slice of the server's
     * name-ordered list, exactly like the inbox. Paging, searching and filtering all
     * happen in SQL -- this page never holds the participant table.
     */
    const [recipients, setRecipients] = useState<Recipient[]>([]);
    /** How many recipients match the filters, loaded or not. */
    const [recipientsTotal, setRecipientsTotal] = useState(0);
    const [loadingRecipients, setLoadingRecipients] = useState(false);
    const [loadingMoreRecipients, setLoadingMoreRecipients] = useState(false);
const [cohorts, setCohorts] = useState<Cohort[]>([]);
    const [searchTerm, setSearchTerm] = useState('');
    /** The search the server has actually answered; typing alone searches nothing. */
    const [appliedSearch, setAppliedSearch] = useState('');
    // A cohort key rather than a cohort, because the Select it drives carries a string. `ALL_COHORTS`
    // is the sentinel for "show everybody", which is not the same as "show one cohort".
    const [selectedCohortKey, setSelectedCohortKey] = useState(ALL_COHORTS);
    /**
     * Whether the picker widens to the viewer's own gender. Off by default: the server
     * answers with "not my gender" unless this is on.
     */
    const [includeSameGender, setIncludeSameGender] = useState(false);
    const [selectedRecipient, setSelectedRecipient] = useState<Recipient | null>(null);
    const [isUnavailableConfirmOpen, setIsUnavailableConfirmOpen] = useState(false);
    const [isSending, setIsSending] = useState(false);

    /**
     * One recipient page from the server: the next `RECIPIENT_PAGE_SIZE` people the current
     * filters match, starting at `skip`.
     *
     * `filters` overrides let a handler that has just changed a filter read with the new
     * value in the same click, rather than with state React has not re-rendered yet.
     * `append` grows the window downwards, so a "load more" click adds beneath the rows
     * already on screen and the reader's place never moves.
     */
    const loadRecipientsPage = async (
        skip: number,
        append: boolean,
        filters?: { search?: string; cohortKey?: string; includeSameGender?: boolean }
    ) => {
        const search = filters?.search ?? appliedSearch;
        const cohort = cohortFromKey(filters?.cohortKey ?? selectedCohortKey);
        const widenGender = filters?.includeSameGender ?? includeSameGender;
        if (append) {
            setLoadingMoreRecipients(true);
        } else {
            setLoadingRecipients(true);
        }
        try {
            const page = await getLoveNoteRecipients({
                skip,
                limit: RECIPIENT_PAGE_SIZE,
                ...(search.trim() ? { q: search.trim() } : {}),
                includeSameGender: widenGender,
                ...(cohort ? { year: cohort.year, department: cohort.department } : {}),
            });
            setRecipients((previous) => (append ? [...previous, ...page.users] : page.users));
            setRecipientsTotal(page.total);
        } catch (error) {
            console.error("Failed to load recipients:", error);
            toast.error("Could not load recipients. Please try again.");
        } finally {
            setLoadingRecipients(false);
            setLoadingMoreRecipients(false);
        }
    };

    /**
     * One inbox read into state: the rows replace or extend the window, and the total
     * travels with them. A legacy bare array hands back one unfiltered list, which lands
     * whole with no pager to grow it.
     */
    const applyInboxPage = (answer: unknown, append: boolean) => {
        const rows = inboxRowsOf(answer);
        const enveloped = !Array.isArray(answer);
        if (append) {
            setReceivedNotes((prev) => [...prev, ...rows]);
        } else {
            setReceivedNotes(rows);
        }
        setInboxTotal(inboxTotalOf(answer, rows.length));
        setPagedInbox(enveloped);
    };

    /** One inbox page from the server: the next `limit` notes newest-first. */
    const readInboxPage = (skip: number, limit: number) =>
        getReceivedLoveNotes({ skip, limit });

    /**
     * Grow the inbox downwards: the next page appends beneath the notes already on
     * screen, so the reader's place never moves.
     */
    const loadMoreInbox = async () => {
        if (loadingMoreNotes) return;
        setLoadingMoreNotes(true);
        try {
            applyInboxPage(await readInboxPage(receivedNotes.length, INBOX_PAGE_SIZE), true);
        } catch (error) {
            console.error("Failed to load more love notes:", error);
            toast.error("Could not load more love notes.");
        } finally {
            setLoadingMoreNotes(false);
        }
    };


// Used to fetch templates, cohorts, and the first inbox page
    useEffect(() => {
        const fetchInitialData = async () => {
            // Claimed before anything awaits, so the tab effect below can tell "not
            // loaded yet" from "loaded empty" and never re-reads the first page.
            setLoadingNotes(true);
            try {
                // Fetch templates
                const templatePromises = Array.from({ length: 6 }, (_, i) =>
                    fetch(`/templates/template_${i + 1}.json`).then(res => res.json())
                );
                const loadedTemplates = await Promise.all(templatePromises);
                setTemplates(loadedTemplates);
                setSelectedTemplate(loadedTemplates[0]);

                // Fetch cohorts and the first inbox page in parallel. Only the first
                // inbox page travels here: the inbox grows through "show more", not by
                // loading every note behind a card grid. The recipient list is not read
                // at all -- the picker reads it one page at a time when it is opened.
                const [cohortsData, notesPage] = await Promise.all([
                    getCohorts(),
                    getReceivedLoveNotes({ skip: 0, limit: INBOX_PAGE_SIZE })
                ]);
                setCohorts(cohortsData);
                applyInboxPage(notesPage, false);

            } catch (error) {
                console.error("Failed to load initial data:", error);
                toast.error("Could not load necessary data. Please try again later.");
            } finally {
                setLoadingNotes(false);
            }
        };

        fetchInitialData();
    }, []);

    // Used to refresh an empty inbox when the reader opens the tab: a note may have
    // arrived while they were on the create tab. Fires only on the flip edge into the
    // tab, never on the list or loading flags -- an empty inbox is a steady state, and
    // refetching it on every loading toggle re-reads forever. Skipped while the mount
    // fetch is still in flight, and afterwards the mount has already answered, so this
    // reads at most once per visit.
    const prevTabRef = useRef(activeTab);
    useEffect(() => {
        const visitingInbox = activeTab === 'inbox' && prevTabRef.current !== 'inbox';
        prevTabRef.current = activeTab;
        if (visitingInbox && receivedNotes.length === 0 && !loadingNotes) {
            const fetchReceivedNotes = async () => {
                try {
                    setLoadingNotes(true);
                    applyInboxPage(await readInboxPage(0, INBOX_PAGE_SIZE), false);
                } catch (error) {
                    console.error("Failed to load received notes:", error);
                    toast.error("Could not load your love notes.");
                } finally {
                    setLoadingNotes(false);
                }
            };

            fetchReceivedNotes();
        }
    }, [activeTab]);

    // A notification about a received love note arrives here as
    // /love-notes?tab=inbox&note=<note_id>. The reader clicked it to read the note, so
    // both params are honoured: switch to the inbox tab, because the note is only visible
    // there, and open it once the list has loaded.
    //
    // The open is keyed off `receivedNotes` rather than done inside the fetch above, so it
    // works whether the list was already loaded from a previous visit or arrives now. If the
    // id is not in the window yet, further pages are read until it turns up or the inbox
    // runs out -- bounded by the total, so a note taken down since the notification was
    // written ends with nothing opening and the list rendering as usual, which is the
    // honest outcome.
    useEffect(() => {
        const noteId = searchParams.get('note');
        if (!noteId) return;

        // `tab=inbox` is implied by a note id -- the note lives nowhere else -- so it is
        // honoured whether or not the notification happened to include it.
        if (searchParams.get('tab') !== 'inbox') {
            const next = new URLSearchParams(searchParams);
            next.set('tab', 'inbox');
            setSearchParams(next, { replace: true });
        }
        setActiveTab('inbox');

        // Both are cleared together, once the note has actually been opened. Clearing the
        // params before the fetch resolves would reopen it on any later re-render of the
        // list; clearing them after means closing the image does not close the tab.
        if (loadingNotes || receivedNotes.length === 0) return;
        const note = receivedNotes.find((n) => n.id === noteId);
        if (!note) {
            if (pagedInbox && receivedNotes.length < inboxTotal && !loadingMoreNotes) {
                void loadMoreInbox();
            }
            return;
        }

        setViewingImageUrl(note.image_url);

        const next = new URLSearchParams(searchParams);
        next.delete('note');
        next.delete('tab');
        setSearchParams(next, { replace: true });
    }, [searchParams, setSearchParams, loadingNotes, receivedNotes, pagedInbox, inboxTotal, loadingMoreNotes]);

    // Used to open the tab named by ?tab= on a plain link with no note id, e.g. a plain
    // "go to my inbox" link. Only applied when it is a tab this page actually has, so a
    // typo in a link cannot leave the page showing a tab that does not exist.
    useEffect(() => {
        if (searchParams.get('note')) return;
        if (searchParams.get('tab') === 'inbox') setActiveTab('inbox');
    }, [searchParams]);

    // The template's `body` field, which is the rectangle the message is drawn into. This is the
    // authoritative limit for the message: see `effectiveBodyLimit`.
    //
    // `fields` is optional-chained as well as `selectedTemplate`, because the template list is
    // whatever the JSON files contain: a failed fetch resolves to something that is not a
    // template at all, and the canvas effect shrugs that off because it has already returned on a
    // null context. Reading `.fields` unguarded here took down the whole page instead.
    const bodyField = useMemo<TemplateField | null>(
        () => selectedTemplate?.fields?.find((field) => field.name === 'body') ?? null,
        [selectedTemplate]
    );

    // The font the message is drawn in, in the shape `loveNoteText` wants. Kept next to the
    // body field rather than built inside the canvas effect because the limit below needs it
    // during render too, and building it twice invites the two copies to disagree.
    const bodyFont = useMemo<FontStyle>(
        () => ({
            ...fontForPreset(selectedStylePreset),
            family: selectedFont,
        }),
        [selectedFont, selectedStylePreset]
    );

    // The colour the message is drawn in. The card's own declared colour until the sender picks
    // one, and an explicit pick is never silently discarded -- dropping it because they changed
    // card would throw away a deliberate choice over something they did not ask about.
    const cardDefaultColor = bodyField?.font?.color ?? '#000000';
    const bodyColor = colorOverride ?? cardDefaultColor;

    // Measures against the detached canvas, so it is available before the preview has loaded.
    const measureFor = useCallback(
        (font: FontStyle, size: number, text: string): number => {
            const context = measureCanvasRef.current?.getContext('2d');
            if (!context) return 0;
            context.font = fontString(font, size);
            return context.measureText(text).width;
        },
        []
    );

    // How many characters this particular card can actually show, at the smallest size the
    // renderer will use. This is what the sender is held to, and it is why two templates can
    // have different limits: the boxes differ, so the cards do.
    //
    // Computed from the message the sender has typed rather than from a fixed sample, because
    // capacity is a property of the glyphs -- a card holds materially fewer emoji than Latin
    // letters. `fontKey` is in the dependency list so that changing the font or its weight
    // recomputes it.
    const fontKey = `${bodyFont.style}|${bodyFont.weight}|${bodyFont.family}`;
    const capacity = useMemo(() => {
        if (!bodyField) return LOVE_NOTE_BODY_MAX;
        return Math.min(
            LOVE_NOTE_BODY_MAX,
            maxFittingCharacters(
                measureFor,
                bodyFont,
                noteMessage,
                bodyField,
                LOVE_NOTE_BODY_MAX
            )
        );
        // `fontKey` stands in for the three fields of `bodyFont`, which is a fresh object every
        // render and would defeat the memo.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [bodyField, fontKey, measureFor, noteMessage]);

    // The limit actually applied: the smaller of what the server will store and what this card
    // can draw. Neither alone is right -- the ceiling without the card would let the sender
    // type text that gets cut off, and the card alone would let them type something the server
    // refuses at send time with a 422.
    const effectiveBodyLimit = Math.min(LOVE_NOTE_BODY_MAX, capacity);

    // Kept in step when the card or the font changes, because switching to a template with a
    // smaller box can make the existing message over-long. Trimming here rather than only in the
    // handler means the textarea and the preview never disagree about what the note contains.
    useEffect(() => {
        const characters = [...noteMessage];
        if (characters.length <= effectiveBodyLimit) return;
        setNoteMessage(characters.slice(0, effectiveBodyLimit).join(''));
    }, [noteMessage, effectiveBodyLimit]);

    // Used to draw the content on the canvas
    useEffect(() => {
        if (!selectedTemplate || !canvasRef.current) return;

        const canvas = canvasRef.current;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;

        const img = new Image();
        img.src = `/templates/${selectedTemplate.image}`;
        img.onload = () => {
            canvas.width = img.width;
            canvas.height = img.height;
            ctx.drawImage(img, 0, 0);

            // A canvas only measures text in the font currently set on it, so every candidate
            // size has to be applied before it can be measured. Centralised here because the fit
            // probes a dozen sizes per field and the alternative -- remembering to set the font
            // at each one -- is exactly the kind of omission that silently measures at the wrong
            // size and returns a layout that overflows.
            const measureAt = (font: FontStyle, size: number, text: string): number => {
                ctx.font = fontString(font, size);
                return ctx.measureText(text).width;
            };

            // Used to render dynamic fields on the template
            selectedTemplate.fields.forEach(field => {
                const isBody = field.name === 'body';

                // Only the message takes the sender's colour. The signature fields are card
                // chrome drawn in the weight and colour the card was designed around, and the
                // emoji is a colour glyph that ignores `fillStyle` anyway.
                ctx.fillStyle = isBody ? bodyColor : field.font?.color || '#000000';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';

                let textToRender = '';
                let fontWeight = 'normal';
                const fontFamily = selectedFont;
                const fontSize = field.font?.size || 16;

                switch (field.name) {
                    case 'body':
                        textToRender = noteMessage || 'Your message here...';
                        // Its slant and weight come from `bodyFont` below. Deriving them here as
                        // well used to be the only way to express bold *and* italic, and having
                        // two mappings of the same setting is how they drift apart.
                        break;
                    case 'from_label':
                        textToRender = field.value || 'From,';
                        fontWeight = '900'; // Enforce extra bold
                        break;
                    case 'sender_name':
                        textToRender = isAnonymous ? 'Anonymous' : (user?.name || 'Your name');
                        fontWeight = '900'; // Enforce extra bold
                        break;
                    case 'emoji':
                        if (selectedEmoji) {
                            const originalFont = ctx.font;
                            ctx.font = `120px sans-serif`;
                            ctx.fillText(selectedEmoji, field.x + field.width / 2, field.y + field.height / 2);
                            ctx.font = originalFont;
                        }
                        return; 
                }

                // The body field reuses the font and the box computed during render, so the
                // limit the sender was held to and the layout being drawn cannot come from two
                // different readings of the template. The others are always upright: the sender's
                // chosen slant is meant for their message, and tilting a printed "From," or their
                // own name is a different decision from the one they made.
                const font: FontStyle =
                    field.name === 'body'
                        ? bodyFont
                        : { style: 'normal', weight: fontWeight, family: fontFamily };

                // Only `body` is fitted. The signature fields are a fixed word and the sender's
                // own name, and they are drawn at exactly the size the card was drawn around --
                // shrinking them would make a note look wrong for a reason that has nothing to
                // do with its message. They are still wrapped, though, because a long name used
                // to wrap here and drawing it as one overflowing line would be a regression
                // disguised as a simplification.
                // `truncated` is deliberately not read here. It can only be true for a message
                // over the server's cap -- which this field will not let anyone type, see
                // `handleMessageChange` -- or for one unbroken run of glyphs wider than the box,
                // and the counter above is the honest report in both cases. Surfacing it would
                // mean threading canvas-load state back into render for a branch the sender
                // cannot reach.
                // The ceiling is one moderate constant, not `field.font.size` (every template
                // shipped the same declared 32, a placeholder that made a short note look tiny)
                // and not a fraction of the box either -- that made the same two words huge on a
                // tall box and merely large on a short one. See `MAX_FONT_SIZE`. The box still
                // governs everything below the ceiling: the fit comes down from it only when the
                // message cannot otherwise be contained.
                const fitted = isBody                    ? fitTextToBox(
                          measureAt,
                          font,
                          textToRender,
                          field,
                          MAX_FONT_SIZE
                      )
                    : {
                          lines: wrapText(
                              (candidate) => measureAt(font, fontSize, candidate),
                              textToRender,
                              field.width
                          ),
                          fontSize,
                          truncated: false,
                      };

                // The draw has to happen at the size the text was *fitted* at. Drawing at
                // `field.font.size` while using lines that were wrapped for a smaller size is
                // what puts text outside the box: the wrap was computed against one font and
                // the glyphs are painted in another.
                ctx.font = fontString(font, fitted.fontSize);

                const lineHeight = fitted.fontSize;
                const totalTextHeight = fitted.lines.length * lineHeight;
                const startY = (field.y + field.height / 2) - (totalTextHeight / 2) + (lineHeight / 2);

                fitted.lines.forEach((line, index) => {
                    ctx.fillText(line, field.x + field.width / 2, startY + (index * lineHeight));
                });
            });
        };

    }, [
        selectedTemplate,
        noteMessage,
        isAnonymous,
        selectedEmoji,
        selectedFont,
        selectedStylePreset,
        user,
        bodyFont,
        bodyColor,
    ]);

    // Used to keep the message inside what this card can actually show. Truncating at a
    // code-point boundary rather than refusing the keystroke is deliberate: the limit is
    // something the sender cannot see coming, and silently dropping input they just typed reads
    // as the field being broken. The alternative -- letting them type past it and surfacing a
    // 422 on send -- spends the whole message to deliver the same result.
    //
    // `effectiveBodyLimit` rather than `LOVE_NOTE_BODY_MAX`: the ceiling is the server's, but
    // the number that matters to the sender is the one their chosen card imposes.
    const handleMessageChange = (value: string) => {
        const characters = [...value];
        setNoteMessage(
            characters.length > effectiveBodyLimit
                ? characters.slice(0, effectiveBodyLimit).join('')
                : value
        );
    };

    // Used to handle the selection of a single emoji sticker.
    const handleEmojiSelect = (emoji: string) => {
        setSelectedEmoji(prev => (prev === emoji ? null : emoji));
    };
    
    // Used to open the recipient modal after validation
    const handleOpenRecipientModal = () => {
        if (user?.love_notes_send_enabled) {
            toast.info("You've already sent your love note this season!");
            return;
        }
        if (!noteMessage.trim()) {
            toast.error('Please write a message before sending!');
            return;
        }
        setIsRecipientModalOpen(true);
        // The first page is read when the picker opens, not at mount: nobody who never
        // sends a note ever pays for a recipient query.
        void loadRecipientsPage(0, false);
    };

    /**
     * Run the typed search against the server. Names are matched in SQL and only the
     * matching page travels back, so the box never filters a list it already holds.
     */
    const submitRecipientSearch = () => {
        const cleaned = searchTerm.trim();
        setAppliedSearch(cleaned);
        void loadRecipientsPage(0, false, { search: cleaned });
    };

    /** Changing a filter re-reads the first page: the rows below it belong to the old one. */
    const handleRecipientCohortChange = (key: string) => {
        setSelectedCohortKey(key);
        void loadRecipientsPage(0, false, { cohortKey: key });
    };

    const handleIncludeSameGenderChange = (checked: boolean) => {
        setIncludeSameGender(checked);
        void loadRecipientsPage(0, false, { includeSameGender: checked });
    };

    // Used to send the note after confirming recipient. A recipient an admin has blocked
    // is still allowed to receive a note, but the sender is asked once to confirm first.
    const handleConfirmAndSend = async () => {
        if (!selectedRecipient) {
            toast.error("Please select a recipient.");
            return;
        }
        if (selectedRecipient.is_blocked) {
            setIsUnavailableConfirmOpen(true);
            return;
        }
        await sendSelectedNote();
    };

    // Used to perform the actual send, once the recipient (and any warning) is settled
    const sendSelectedNote = async () => {
        if (!selectedRecipient || !canvasRef.current) {
            toast.error("Please select a recipient.");
            return;
        }
        // The note is sent at most once per season, so a second click while the first
        // request is still open must not spend it.
        if (isSending) {
            return;
        }

        const imageBase64 = canvasRef.current.toDataURL('image/png');

        // Mirrors the backend's MAX_IMAGE_SIZE_MB cap on the decoded image. base64
        // spends 4 characters per 3 bytes and the data URI prefix only makes this an
        // overestimate, so an over-limit note is caught here rather than after a
        // multi-megabyte upload the server will reject anyway.
        if ((imageBase64.length * 3) / 4 > MAX_NOTE_IMAGE_BYTES) {
            toast.error('This note is too large to send. Please try a different template.');
            return;
        }

        try {
            setIsSending(true);
            await sendLoveNote({
                recipient_id: selectedRecipient.id,
                image_base64: imageBase64,
                body: noteMessage,
                anonymous: isAnonymous,
            });
            toast.success('Love note sent for review! 💕');
            setIsRecipientModalOpen(false);
            setIsUnavailableConfirmOpen(false);
            setNoteMessage('');
            setSelectedEmoji(null);
            setSelectedRecipient(null);
            setSearchTerm('');
            setAppliedSearch('');
            setSelectedCohortKey(ALL_COHORTS);
            setIncludeSameGender(false);
            setRecipients([]);
            setRecipientsTotal(0);
            // Used to refresh user data to get the updated love_notes_send_enabled status
            if (token) {
                fetchAndSetUser(token);
            }
        } catch (error: any) {
            console.error("Failed to send love note:", error);
            // Schema validation failures (422) report `detail` as a list of objects,
            // while a raised HTTPException reports it as a plain string.
            const detail = error.response?.data?.detail;
            const errorMessage = Array.isArray(detail)
                ? detail[0]?.msg
                : detail;
            toast.error(errorMessage || "Failed to send love note. Please try again.");
        } finally {
            setIsSending(false);
        }
    };

    // Used to download the note as an image
    const downloadNote = () => {
        const canvas = canvasRef.current;
        if (canvas) {
            const image = canvas.toDataURL('image/png');
            const link = document.createElement('a');
            link.href = image;
            link.download = `${selectedTemplate?.template_name || 'love-note'}.png`;
            link.click();
            toast.success('Note downloaded! 📱');
        }
    };

    return (
        <>
            <div className="min-h-screen p-2 sm:p-4 pt-20 sm:pt-24 overflow-x-hidden relative">
                <Navigation />
                <FloatingHearts />
                {/* Floral preview: 4536919 centered behind the header box (softened so the
                    heading + subtitle stay legible over it in both themes). */}
                <FloralDecor pair={4} className="left-1/2 top-80 w-[64rem] max-w-[98vw] -translate-x-1/2 -translate-y-1/2 opacity-60" />
                {/* Floral edges: cropped blossom mirrored top-right;
                    1706715_bottom bottom-left (+ vertical-axis mirror
                    bottom-right). */}
                <FloralDecor pair={5} className="right-0 top-0 w-[24rem] -scale-x-100 opacity-90 sm:w-[30rem]" />
                <FloralDecor pair={7} className="bottom-0 left-0 w-[34rem] opacity-90 sm:w-[42rem]" />
                <FloralDecor pair={7} className="bottom-0 right-0 w-[34rem] -scale-x-100 opacity-90 sm:w-[42rem]" />
                {/* Scattered chat glyphs for the side gutters. */}
                <ScatteredIcons variant="chat" />

                {/* Header (above the floral backdrop) */}
                <div className="relative z-10 max-w-7xl mx-auto mb-6 sm:mb-8 px-2 sm:px-4">
                    <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4 sm:gap-6">
                        <div className="text-center lg:text-left">
                            <h1 className="header-halo text-2xl sm:text-3xl md:text-4xl lg:text-5xl font-dancing text-romantic mb-2 break-words">
                                Love Notes & Cards 💌
                            </h1>
                            <p className="page-subtitle text-sm sm:text-base md:text-xl px-2 break-words">
                                Create and share beautiful digital love notes
                            </p>
                        </div>
                        <CountdownTimer />
                    </div>
                </div>

                {/* Tab Navigation */}
                <div className="max-w-7xl mx-auto mb-6 sm:mb-8 px-2 sm:px-4">
                    <div className="flex gap-2 sm:gap-4 justify-center flex-wrap">
                        <Button
                            onClick={() => setActiveTab('create')}
                            variant={activeTab === 'create' ? 'default' : 'outline'}
                            className="font-dancing text-sm sm:text-lg px-4 sm:px-8 h-9 sm:h-10"
                        >
                            <Palette className="w-4 h-4 sm:w-5 sm:h-5 mr-1 sm:mr-2" />
                            Create Note
                        </Button>
                        <Button
                            onClick={() => setActiveTab('inbox')}
                            variant={activeTab === 'inbox' ? 'default' : 'outline'}
                            className="font-dancing text-sm sm:text-lg px-4 sm:px-8 h-9 sm:h-10"
                        >
                            <Mail className="w-4 h-4 sm:w-5 sm:h-5 mr-1 sm:mr-2" />
                            Inbox ({pagedInbox ? inboxTotal : receivedNotes.length})
                        </Button>
                    </div>
                </div>

                <div className="max-w-7xl mx-auto px-2 sm:px-4">
                    {activeTab === 'create' ? (
                        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 sm:gap-8">
                        {/* Left Column: Editor */}
                        <div className="lg:col-span-1 space-y-4 sm:space-y-6">
                            <Card className="confession-card">
                                <CardHeader className="p-4 sm:p-6">
                                    <CardTitle className="text-xl sm:text-2xl font-dancing text-romantic">Customize Your Note</CardTitle>
                                </CardHeader>
                                <CardContent className="space-y-4 sm:space-y-6 p-4 sm:p-6">
                                    {/* Template Selection */}
                                    <div>
                                        <Label className="text-romantic font-semibold mb-3 block text-sm sm:text-base">Choose a Template</Label>
                                        <div className="grid grid-cols-3 gap-2 sm:gap-3">
                                            {templates.map((template) => (
                                                <button
                                                    key={template.template_name}
                                                    onClick={() => setSelectedTemplate(template)}
                                                    className={`p-2 rounded-lg border-2 transition-all duration-300 hover:scale-105 ${selectedTemplate?.template_name === template.template_name
                                                            ? 'border-romantic'
                                                            : 'border-muted hover:border-romantic'
                                                        }`}
                                                >
                                                    <img src={`/templates/${template.image}`} alt={template.template_name} className="w-full h-auto rounded" />
                                                </button>
                                            ))}
                                        </div>
                                    </div>

                                    {/* Font Selection */}
                                    <div>
                                        <Label className="text-romantic font-semibold mb-3 block">Choose a Font</Label>
                                        <div className="flex flex-wrap gap-2">
                                            {availableFonts.map((font) => (
                                                <button
                                                    key={font}
                                                    onClick={() => setSelectedFont(font)}
                                                    className={`px-4 py-2 text-sm rounded-lg border-2 transition-all duration-300 hover:scale-105 ${selectedFont === font
                                                            ? 'border-romantic bg-gradient-love text-white'
                                                            : 'border-muted hover:border-romantic'
                                                        }`}
                                                    style={{ fontFamily: font }}
                                                >
                                                    {font}
                                                </button>
                                            ))}
                                        </div>
                                    </div>

                                    {/* Font Style Selection */}
                                    <div>
                                        <Label className="text-romantic font-semibold mb-3 block">Choose a Style</Label>
                                        <div className="flex flex-wrap gap-2">
                                            {STYLE_PRESETS.map((preset) => (
                                                <button
                                                    key={preset.value}
                                                    onClick={() => setSelectedStylePreset(preset.value)}
                                                    className={`px-4 py-2 text-sm rounded-lg border-2 transition-all duration-300 hover:scale-105 ${selectedStylePreset === preset.value
                                                            ? 'border-romantic bg-gradient-love text-white'
                                                            : 'border-muted hover:border-romantic'
                                                        }`}
                                                    // The button is drawn in the style it selects,
                                                    // so "Bold Italic" reads as bold italic rather
                                                    // than as a label for it.
                                                    style={{
                                                        fontWeight: preset.preview.weight,
                                                        fontStyle: preset.preview.style
                                                    }}
                                                >
                                                    {preset.label}
                                                </button>
                                            ))}
                                        </div>
                                    </div>

                                    {/* Message Colour Selection */}
                                    <div>
                                        <Label className="text-romantic font-semibold mb-3 block">Choose a Colour</Label>
                                        <div className="flex flex-wrap items-center gap-2">
                                            {noteTextColors.map((color) => (
                                                <button
                                                    key={color.value}
                                                    onClick={() => setColorOverride(color.value)}
                                                    title={color.label}
                                                    aria-label={color.label}
                                                    aria-pressed={bodyColor === color.value}
                                                    className={`w-9 h-9 rounded-full border-2 transition-all duration-300 hover:scale-110 ${bodyColor === color.value
                                                            ? 'border-romantic ring-2 ring-offset-2 ring-romantic'
                                                            : 'border-muted hover:border-romantic'
                                                        }`}
                                                    style={{ backgroundColor: color.value }}
                                                />
                                            ))}
                                            {/* Back to whatever the card was designed with. Only worth
                                                showing when the sender has actually overridden it,
                                                and when the card's colour is not one of the swatches
                                                above -- template 5's `#F0F4FF` is not. */}
                                            {bodyColor !== cardDefaultColor && (
                                                <button
                                                    onClick={() => setColorOverride(null)}
                                                    className="px-3 py-2 text-xs rounded-lg border-2 border-dashed border-muted hover:border-romantic transition-all duration-300"
                                                    style={{ color: cardDefaultColor }}
                                                >
                                                    Card default
                                                </button>
                                            )}
                                        </div>
                                    </div>

{/* Message Input */}
                                    <div>
                                         <Label htmlFor="message" className="text-romantic font-semibold">Your Message</Label>
                                         <Textarea
                                            id="message"
                                            placeholder="Write your heartfelt message here..."
                                            value={noteMessage}
                                            onChange={(e) => handleMessageChange(e.target.value)}
                                            rows={4}
                                            className="resize-none"
                                        />
                                         <p className="text-xs text-muted-foreground mt-1">
                                            {characterCount(noteMessage)} / {effectiveBodyLimit} characters
                                         </p>
                                         <p className="text-xs text-muted-foreground mt-1">
                                            {capacity < LOVE_NOTE_BODY_MAX
                                                ? `This card fits about ${capacity} characters. Longer notes are written smaller so the whole message fits.`
                                                : 'Longer notes are written smaller so the whole message fits the card.'}
                                         </p>
                                    </div>

                                    {/* Emoji Stickers */}
                                    <div>
                                        <Label className="text-romantic font-semibold mb-3 block">Add a Sticker</Label>
                                        <div className="grid grid-cols-8 gap-2">
                                            {emojiStickers.map((emoji) => (
                                                <button
                                                    key={emoji}
                                                    onClick={() => handleEmojiSelect(emoji)}
                                                    className={`w-10 h-10 text-lg rounded-lg border-2 transition-all duration-300 hover:scale-110 ${selectedEmoji === emoji
                                                            ? 'border-romantic bg-gradient-love'
                                                            : 'border-muted hover:border-romantic'
                                                        }`}
                                                >
                                                    {emoji}
                                                </button>
                                            ))}
                                        </div>
                                    </div>
                                </CardContent>
                            </Card>
                        </div>

                        {/* Center Column: Live Preview */}
                        <div className="lg:col-span-2 space-y-6">
                            <Card className="confession-card sticky top-24">
                                <CardHeader>
                                    <CardTitle className="text-2xl font-dancing text-romantic">Live Preview</CardTitle>
                                </CardHeader>
                                <CardContent>
                                    <canvas 
                                        ref={canvasRef} 
                                        className="w-full h-auto rounded-lg border-2 border-muted shadow-lg cursor-pointer hover:opacity-90 transition-opacity" 
                                        onClick={() => {
                                            if (canvasRef.current) {
                                                const imageUrl = canvasRef.current.toDataURL('image/png');
                                                setViewingImageUrl(imageUrl);
                                            }
                                        }}
                                    />
                                    <div className="flex items-center justify-between mt-4">
                                        <div className="flex items-center space-x-2">
                                            <Switch id="anonymous-switch" checked={isAnonymous} onCheckedChange={setIsAnonymous} />
                                            <Label htmlFor="anonymous-switch" className="text-romantic font-semibold">Send Anonymously</Label>
                                        </div>
                                        <Button
                                            onClick={downloadNote}
                                            variant="outline"
                                            className="font-dancing"
                                        >
                                            <Download className="w-4 h-4 mr-2" />
                                            Download
                                        </Button>
                                    </div>
                                    <Button
                                        onClick={handleOpenRecipientModal}
                                        disabled={user?.love_notes_send_enabled}
                                        className="w-full font-dancing text-lg py-3 mt-4"
                                    >
                                        <Send className="w-5 h-5 mr-2" />
                                        {user?.love_notes_send_enabled ? "You've Already Sent Your Note" : "Send Your Love Note"}
                                    </Button>
                                </CardContent>
                            </Card>
                        </div>
                    </div>
                    ) : (
                        /* Inbox */
                        <div className="space-y-6">
                            {loadingNotes ? (
                                <div className="flex justify-center items-center py-20">
                                    <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-romantic"></div>
                                </div>
                            ) : receivedNotes.length === 0 ? (
                                <Card className="confession-card">
                                    <CardContent className="py-16 text-center">
                                        <Mail className="w-16 h-16 mx-auto mb-4 text-muted-foreground" />
                                        <p className="text-lg text-muted-foreground">
                                            No love notes yet. Check back later! 💌
                                        </p>
                                    </CardContent>
                                </Card>
                            ) : (
                                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                                    {receivedNotes.map((note) => (
                                        <Card
                                            key={note.id}
                                            className="confession-card hover:scale-105 transition-all duration-300 overflow-hidden"
                                        >
                                            <CardContent className="p-0">
                                                <div className="relative overflow-hidden">
                                                    <img 
                                                        src={note.image_url} 
                                                        alt="Love note" 
                                                        // The URL is a non-expiring signed bearer: never send it
                                                        // as a referrer to the CDN or anywhere else.
                                                        referrerPolicy="no-referrer"
                                                        className="w-full h-48 object-cover cursor-pointer"
                                                        onClick={() => setViewingImageUrl(note.image_url)}
                                                    />
                                                    <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/70 to-transparent p-4">
                                                        <div className="text-white">
                                                            <MessageCircle className="w-6 h-6 mb-1" fill="currentColor" />
                                                            <p className="text-sm font-dancing">From: {note.from}</p>
                                                        </div>
                                                    </div>
                                                </div>
                                                <div className="p-4 space-y-3">
<p className="text-sm text-muted-foreground line-clamp-2">{note.body}</p>
                                                    {note.created_at && (
                                                        <p className="text-xs text-muted-foreground">
                                                            {formatDateTimeDDMMYYYY(note.created_at)}
                                                        </p>
                                                    )}
                                                    {reportedNoteIds.includes(note.id) && (
                                                        <p className="text-xs text-muted-foreground">
                                                            Reported — our team will review it.
                                                        </p>
                                                    )}
                                                    <div className="flex gap-2 pt-2">
                                                        <Button
                                                            onClick={() => setViewingImageUrl(note.image_url)}
                                                            variant="outline"
                                                            size="sm"
                                                            className="flex-1"
                                                        >
                                                            Open
                                                        </Button>
                                                        <Button
                                                            onClick={() => {
                                                                const link = document.createElement('a');
                                                                link.href = note.image_url;
                                                                link.download = `love-note-${note.id}.png`;
                                                                link.target = '_blank';
                                                                // Navigating to a bearer URL must not send a referrer.
                                                                link.rel = 'noreferrer';
                                                                document.body.appendChild(link);
                                                                link.click();
                                                                document.body.removeChild(link);
                                                                toast.success('Download started! 📥');
                                                            }}
                                                            variant="outline"
                                                            size="sm"
                                                            className="flex-1"
                                                        >
                                                            <Download className="w-4 h-4 mr-1" />
                                                            Download
                                                        </Button>
                                                        {!reportedNoteIds.includes(note.id) && (
                                                            <Button
                                                                onClick={() => setNoteToReport(note)}
                                                                variant="ghost"
                                                                size="sm"
                                                                className="text-muted-foreground hover:text-destructive"
                                                                title="Report this love note"
                                                            >
                                                                <Flag className="w-4 h-4" />
                                                            </Button>
                                                        )}
                                                    </div>
                                                </div>
                                            </CardContent>
                                        </Card>
                                    ))}
                                </div>
                            )}
                            {/* The grid only grows downwards: the next page appends beneath the
                                notes already on screen, so the reader's place never moves. Shown
                                only while the inbox holds more than the grid is showing; legacy
                                answers never page. */}
                            {pagedInbox && receivedNotes.length < inboxTotal && (
                                <div className="flex justify-center pt-2">
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={() => void loadMoreInbox()}
                                        disabled={loadingMoreNotes}
                                        aria-label="Show more love notes"
                                    >
                                        {loadingMoreNotes
                                            ? 'Loading…'
                                            : `Show more (${inboxTotal - receivedNotes.length} remaining)`}
                                    </Button>
                                </div>
                            )}
                        </div>
                    )}
                </div>
            </div>

<ReportLoveNoteDialog
                isOpen={noteToReport !== null}
                onClose={() => setNoteToReport(null)}
                noteId={noteToReport?.id ?? ''}
                noteText={noteToReport?.body ?? ''}
                onReported={(noteId) =>
                    setReportedNoteIds((previous) =>
                        previous.includes(noteId) ? previous : [...previous, noteId]
                    )
                }
            />

            {/* Recipient Selection Modal */}
            <Dialog open={isRecipientModalOpen} onOpenChange={setIsRecipientModalOpen}>
                <DialogContent className="sm:max-w-[425px] md:max-w-lg lg:max-w-xl confession-card">
                    <DialogHeader>
                        <DialogTitle className="font-dancing text-3xl text-romantic">Select Your Recipient</DialogTitle>
                        <DialogDescription>
                            Find the special someone to send your love note to.
                        </DialogDescription>
                    </DialogHeader>
                    <div className="space-y-4 py-4">
                        {/* Important Information */}
                        <div className="bg-yellow-100 border-l-4 border-yellow-500 text-yellow-700 p-4 rounded-md" role="alert">
                            <p className="font-bold flex items-center"><Info className="w-5 h-5 mr-2" />Important</p>
                            <ul className="list-disc list-inside mt-2 text-sm space-y-1">
                                <li>You can only send <strong>one</strong> Love Note this season. Are you sure you want to use it now?</li>
                                <li>All notes are <strong>reviewed by moderators</strong> before being delivered to the recipient.</li>
                            </ul>
                        </div>

                        {/* Filters. Every one of these re-reads a page from the server:
                            the search only runs when submitted, and the cohort and gender
                            controls re-read the first page immediately. */}
                        <div className="space-y-3">
                            <div className="flex gap-2">
                                <div className="relative flex-1">
                                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                                    <Input
                                        placeholder="Search by name..."
                                        value={searchTerm}
                                        onChange={(e) => setSearchTerm(e.target.value)}
                                        // Enter submits the same way the button does, so a search is a
                                        // deliberate act either way -- the box does not re-query per keystroke.
                                        onKeyDown={(e) => {
                                            if (e.key === 'Enter') {
                                                e.preventDefault();
                                                submitRecipientSearch();
                                            }
                                        }}
                                        className="pl-10"
                                    />
                                </div>
                                <Button
                                    type="button"
                                    variant="outline"
                                    onClick={submitRecipientSearch}
                                    aria-label="Search recipients"
                                >
                                    Search
                                </Button>
                            </div>
<Select value={selectedCohortKey} onValueChange={handleRecipientCohortChange}>
                                <SelectTrigger>
                                    <SelectValue placeholder="Filter by cohort" />
                                </SelectTrigger>
                                <SelectContent>
                                    <SelectItem value={ALL_COHORTS}>All</SelectItem>
                                    {cohorts.map(c => {
                                        const key = cohortKey(c);
                                        return (
                                            <SelectItem key={key} value={key}>
                                                {cohortLabel(c.year, c.department)}
                                            </SelectItem>
                                        );
                                    })}
                                </SelectContent>
                            </Select>
                            {/* Off: the server answers with everyone whose gender is not the
                                viewer's. On: it answers with both genders. */}
                            <div className="flex items-center space-x-2">
                                <Switch
                                    id="include-same-gender-switch"
                                    checked={includeSameGender}
                                    onCheckedChange={handleIncludeSameGenderChange}
                                />
                                <Label htmlFor="include-same-gender-switch" className="text-romantic font-semibold">
                                    Include same gender
                                </Label>
                            </div>
                        </div>

                        {/* User List. The window only grows downwards: "load more" appends the next
                            page beneath the rows already read, so the reader's place never moves. */}
                        <ScrollArea className="h-64 border rounded-md p-2">
                            <div className="space-y-2">
                                {loadingRecipients ? (
                                    <div className="flex justify-center items-center py-8">
                                        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-romantic"></div>
                                    </div>
                                ) : recipients.length > 0 ? (
                                    <>
                                        {recipients.map(u => (
                                            <div
                                                key={u.id}
                                                onClick={() => setSelectedRecipient(u)}
                                                className={`flex items-center p-3 rounded-md cursor-pointer transition-all duration-200 ${selectedRecipient?.id === u.id ? 'bg-romantic/20 border-romantic border' : 'hover:bg-muted/50'}`}
                                            >
                                                <ProfileAvatar
                                                    src={u.profile_picture_url}
                                                    name={u.name}
                                                    className="w-10 h-10 mr-4"
                                                />
                                                <div>
                                                    <p className="font-semibold">{u.name}</p>
                                                    <p className="text-sm text-muted-foreground">{cohortLabel(u.year, u.department)}</p>
                                                </div>
                                                {u.is_blocked && (
                                                    <Badge variant="secondary" className="ml-auto">Not available</Badge>
                                                )}
                                            </div>
                                        ))}
                                        {recipients.length < recipientsTotal && (
                                            <div className="flex justify-center pt-2 pb-1">
                                                <Button
                                                    type="button"
                                                    variant="outline"
                                                    size="sm"
                                                    onClick={() => void loadRecipientsPage(recipients.length, true)}
                                                    disabled={loadingMoreRecipients}
                                                    aria-label="Load more recipients"
                                                >
                                                    <ChevronDown className="w-4 h-4 mr-1" />
                                                    {loadingMoreRecipients
                                                        ? 'Loading…'
                                                        : `Load more (${recipientsTotal - recipients.length} remaining)`}
                                                </Button>
                                            </div>
                                        )}
                                    </>
                                ) : (
                                    <p className="text-center text-muted-foreground py-8">No users found.</p>
                                )}
                            </div>
                        </ScrollArea>
                        {selectedRecipient && (
                            <p className="text-center text-sm text-romantic font-semibold">
                                Selected: {selectedRecipient.name}
                            </p>
                        )}
                    </div>
                    <DialogFooter>
                        <Button
                            onClick={handleConfirmAndSend}
                            disabled={!selectedRecipient || isSending}
                            className="w-full font-dancing text-lg py-3"
                        >
                            {isSending ? (
                                <>
                                    <Loader2 className="w-5 h-5 mr-2 animate-spin" />
                                    Sending…
                                </>
                            ) : (
                                <>
                                    <Send className="w-5 h-5 mr-2" />
                                    Confirm & Send Note
                                </>
                            )}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* Confirmation shown for a recipient an admin has blocked. The note is still
                allowed through, so the sender decides rather than the client silently
                dropping it. */}
            <AlertDialog open={isUnavailableConfirmOpen} onOpenChange={setIsUnavailableConfirmOpen}>
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle className="font-dancing text-2xl text-romantic">
                            This profile is currently not available
                        </AlertDialogTitle>
                        <AlertDialogDescription>
                            {selectedRecipient?.name} is currently not available, so they may not be able
                            to receive or reply to your note. Do you still want to send it?
                        </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                        <AlertDialogCancel>Cancel</AlertDialogCancel>
                        <AlertDialogAction
                            onClick={sendSelectedNote}
                            disabled={isSending}
                        >
                            {isSending ? 'Sending…' : 'Send anyway'}
                        </AlertDialogAction>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>

            {/* Full Screen Image Viewer */}
            {viewingImageUrl && (
                <div 
                    className="fixed inset-0 bg-black/90 flex items-center justify-center z-50 p-4"
                    onClick={() => setViewingImageUrl(null)}
                >
                    <div className="max-w-4xl w-full animate-scale-in relative">
                        <Button
                            onClick={() => setViewingImageUrl(null)}
                            variant="ghost"
                            size="sm"
                            className="absolute -top-12 right-0 text-white hover:bg-white/20"
                        >
                            <X className="w-6 h-6" />
                        </Button>
                        <img 
                            src={viewingImageUrl} 
                            alt="Love note" 
                            // Non-expiring signed bearer URL: no referrer, as above.
                            referrerPolicy="no-referrer"
                            className="w-full h-auto rounded-lg shadow-2xl"
                            onClick={(e) => e.stopPropagation()}
                        />
                        <div className="flex justify-center gap-3 mt-4">
                            <Button
                                onClick={(e) => {
                                    e.stopPropagation();
                                    const link = document.createElement('a');
                                    link.href = viewingImageUrl;
                                    link.download = `love-note.png`;
                                    link.target = '_blank';
                                    // Navigating to a bearer URL must not send a referrer.
                                    link.rel = 'noreferrer';
                                    document.body.appendChild(link);
                                    link.click();
                                    document.body.removeChild(link);
                                    toast.success('Download started! 📥');
                                }}
                                variant="outline"
                                className="bg-white/10 text-white hover:bg-white/20"
                            >
                                <Download className="w-4 h-4 mr-2" />
                                Download
                            </Button>
                        </div>
                    </div>
                </div>
            )}

            {/* Old Full Page Note Modal - Keep for live preview functionality */}
            {selectedNote && (
                <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
                    <div className="max-w-2xl w-full animate-scale-in">
                        <div className="relative">
                            <Button
                                onClick={() => setSelectedNote(null)}
                                variant="ghost"
                                size="sm"
                                className="absolute -top-12 right-0 text-white hover:bg-white/20"
                            >
                                <X className="w-5 h-5" />
                            </Button>

                            <Card className="overflow-hidden">
                                <CardContent className="p-0">
                                    <div className="relative">
                                        <img 
                                            src={selectedNote.image_url} 
                                            alt="Love note" 
                                            // Non-expiring signed bearer URL: no referrer, as above.
                                            referrerPolicy="no-referrer"
                                            className="w-full h-auto"
                                        />
                                    </div>
                                    <div className="p-8 space-y-4">
                                        <div className="text-center">
                                            <h2 className="font-dancing text-4xl mb-4 text-romantic">💌 Love Note 💌</h2>
                                            <div className="bg-muted/30 rounded-lg p-6 mb-4">
                                                <p className="text-lg leading-relaxed">{selectedNote.body}</p>
                                            </div>
                                            <div className="space-y-2">
                                                <p className="text-lg font-dancing text-romantic">
                                                    With love from: {selectedNote.from} 💕
                                                </p>
                                                {selectedNote.created_at && (
                                                    <p className="text-sm text-muted-foreground">
                                                        {formatDateTimeDDMMYYYY(selectedNote.created_at)}
                                                    </p>
                                                )}
                                            </div>
                                        </div>
                                        <div className="flex gap-3 justify-center pt-4">
                                            <Button
                                                onClick={() => setViewingImageUrl(selectedNote.image_url)}
                                                variant="outline"
                                            >
                                                View Full Size
                                            </Button>
                                            <Button
                                                onClick={() => {
                                                    const link = document.createElement('a');
                                                link.href = selectedNote.image_url;
                                                link.download = `love-note-${selectedNote.id}.png`;
                                                link.target = '_blank';
                                                // Navigating to a bearer URL must not send a referrer.
                                                link.rel = 'noreferrer';
                                                    document.body.appendChild(link);
                                                    link.click();
                                                    document.body.removeChild(link);
                                                    toast.success('Download started! 📥');
                                                }}
                                                variant="outline"
                                            >
                                                <Download className="w-4 h-4 mr-2" />
                                                Download
                                            </Button>
                                        </div>
                                    </div>
                                </CardContent>
                            </Card>
                        </div>
                    </div>
                </div>
            )}
        </>
    );
};
