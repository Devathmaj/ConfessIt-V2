/**
 * Turning an API failure into something a person can read.
 *
 * The reason this exists: a FastAPI 422 does not put a sentence in `detail`, it puts an *array*
 * of Pydantic error objects -- `{ type, loc, msg, input, ctx }` per failed field. Code that
 * reaches for `error.response.data.detail` and hands it straight to a toast therefore works
 * for every error the server describes in prose and crashes for every 422, because React is
 * being asked to render an array of plain objects as children. That is a hard crash, not a
 * degraded message: it takes the surrounding view down with it.
 *
 * So this is not a convenience. The admin payloads set `extra="forbid"` and every field is
 * length-capped on the server, which means 422s are a routine part of this API's contract
 * rather than an exceptional case, and a view that cannot render one is a view that goes
 * blank on a routine mistake.
 */

/** One entry of FastAPI's 422 `detail` array. Only the parts we render are typed. */
interface PydanticError {
    loc?: (string | number)[];
    msg?: string;
}

/** The shape shared by every Axios error this app handles. */
interface ErrorWithResponse {
    response?: {
        status?: number;
        data?: {
            detail?: string | PydanticError[];
        };
    };
    message?: string;
}

const isErrorRecord = (value: unknown): value is ErrorWithResponse =>
    typeof value === 'object' && value !== null;

/**
 * A field path as the operator wrote it, not as Pydantic indexed it.
 *
 * `loc` for a body field is `["body", "regno"]`, and for a nested one it is a path
 * (`["body", "preferences", "notifications_enabled"]`). The leading `"body"`/`"query"`/
 * `"path"` segment is transport, not address, so it is dropped; what is left is joined with
 * dots so a reader can tell which field is at fault.
 */
const fieldLabel = (loc?: (string | number)[]): string => {
    if (!loc?.length) return '';
    const path = loc.map(String);
    const startsAtField =
        path[0] === 'body' || path[0] === 'query' || path[0] === 'path' || path[0] === 'header';
    const fields = startsAtField ? path.slice(1) : path;
    return fields.join('.');
};

/**
 * Pydantic's own message, with the noise stripped off the front of it.
 *
 * The raw text is a sentence written for a developer -- "String should have at most 100
 * characters", "Input should be 'male', 'female' or 'other'" -- and for the closed-vocabulary
 * cases it is more reliable than anything this function could reconstruct, so it is kept and
 * only the leading "Value error, " prefix is dropped.
 */
const cleanMessage = (msg?: string): string => {
    if (!msg) return '';
    return msg.replace(/^Value error,\s*/i, '').trim();
};

/**
 * One sentence naming what the server refused, or `null` when it said nothing usable.
 *
 * Deliberately reports every failed field rather than only the first. An admin create form can
 * be wrong in more than one way at once, and reporting one field per round-trip teaches the
 * operator to fix and resubmit repeatedly for something they could have been told at once.
 */
export const describeValidationDetail = (detail?: string | PydanticError[]): string | null => {
    if (typeof detail === 'string') {
        return detail.trim() || null;
    }
    if (!Array.isArray(detail)) return null;

    const parts = detail
        .map((error) => {
            const field = fieldLabel(error?.loc);
            const message = cleanMessage(error?.msg);
            if (field && message) return `${field}: ${message}`;
            return field || message || '';
        })
        .filter(Boolean);

    if (!parts.length) return null;
    // Two errors on the same field are common (too long *and* the wrong character), and the
    // set keeps the toast to one line per distinct cause instead of repeating a field name.
    return Array.from(new Set(parts)).join('; ');
};

/** The HTTP status carried by an Axios failure, when there is one. */
export const apiErrorStatus = (error: unknown): number | undefined => {
    if (!isErrorRecord(error)) return undefined;
    return error.response?.status;
};

/** The server's `detail` rendered as text, or `null` when it said nothing usable. */
export const describeApiErrorDetail = (error: unknown): string | null => {
    if (!isErrorRecord(error)) return null;
    return describeValidationDetail(error.response?.data?.detail);
};

/**
 * The best available human-readable explanation of a failed request.
 *
 * `fallback` is required so that a caller always has something to show: the paths through this
 * are a network drop, a proxy HTML error page, a JSON body with no `detail`, and the 422 array,
 * and none of the four may end in a blank toast.
 */
export const describeApiError = (error: unknown, fallback: string): string => {
    if (!isErrorRecord(error)) return fallback;

    const detail = error.response?.data?.detail;

    if (typeof detail === 'string') {
        const text = detail.trim();
        if (!text) return fallback;
        // A reverse proxy in front of the app answers with its own HTML body, which arrives
        // here as a very long string. Returned rather than merely skipped, because falling
        // through would hand the same string to `describeValidationDetail`, which accepts any
        // string and would hand it straight back.
        if (text.startsWith('<')) return fallback;
        return text;
    }

    const described = describeValidationDetail(detail);
    if (described) return described;

    const message = typeof error.message === 'string' ? error.message.trim() : '';
    if (message && message !== 'Network Error') return message;

    return fallback;
};