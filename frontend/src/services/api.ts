// src/services/api.ts -- barrel over split domain modules (see api/*).
//
// Split incrementally to keep all 50+ `@/services/api` importers working: auth lives in
// `./api/auth` (session + 401 retry interceptor, imported here for its side effect so the
// interceptor registers exactly once); confessions/games/admin follow the same pattern as
// they are extracted. New code should import from `@/services/api/<domain>` directly.

import axios from 'axios';
import { API_URL } from '@/lib/apiBase';
import { SESSION_REQUEST_TIMEOUT_MS } from '@/lib/apiTimeouts';
import { getAccessToken } from '@/lib/tokenStore';
import type { AccountType } from '@/lib/capabilities';
import type { Cohort, UserGender, UserYear } from '@/lib/accountFields';

// Auth domain. Imported (not just re-exported) because the endpoint helpers below
// call getAuthHeaders/getAccessToken directly -- `export ... from` alone would leave
// those names unbound in this module (runtime ReferenceError). The import also runs
// ./api/auth once, which registers the 401 retry interceptor as a side effect.
import {
  setInMemoryToken,
  getInMemoryToken,
  clearInMemoryToken,
  getAuthHeaders,
  refreshAccessToken,
  serverLogout,
  handleResponseError,
  isAuthRoute,
  isTokenRejected,
} from './api/auth';
export {
  setInMemoryToken,
  getInMemoryToken,
  clearInMemoryToken,
  getAuthHeaders,
  refreshAccessToken,
  serverLogout,
  handleResponseError,
  isAuthRoute,
  isTokenRejected,
};

// The API origin lives in `@/lib/apiBase` -- one variable for the whole app.

// Credentials (the HttpOnly refresh cookie, scoped to `Path=/auth`) travel only on the
// requests that need them, never globally. A global `withCredentials` would attach the
// refresh and client cookies to every cross-origin call -- including all the Bearer-only
// API requests -- widening both CSRF reach and what an XSS on an allowed origin could do
// with the session. The two `fetch` calls that open a session (magic-link request and
// redemption) already opt in per-request with `credentials: 'include'`; the two axios
// calls in ./api/auth are the only other ones that carry cookies.
// Contract (flags, rotation, clearing) is asserted backend-side in
// backend/tests/test_refresh_cookie_contract.py -- this comment restates it, that test
// enforces it. Deliberately not `__Host-`/`Strict`: `__Host-` requires `Path=/`
// (widening), and `Strict` would break cross-site refresh; see `_refresh_cookie_options`.

// --- Confession Endpoints ---

export interface ConfessionsPage {
  confessions: any[];
  total: number;
  skip: number;
  limit: number;
}

/**
 * One page of the participant feed in the requested order, with the total beside it.
 *
 * The sort picker is a SQL ORDER BY and every page a SQL LIMIT/OFFSET on the server --
 * with only the page's comments, authors and reporter ids attached -- so a feed with
 * thousands of posts returns at most one page per request instead of the whole table.
 * Accepts both the new `{confessions, total, skip, limit}` envelope and a legacy bare
 * array (older servers and unit-test mocks), normalising the latter to a single page
 * whose total is the array length.
 */
export interface ConfessionCommentsPage {
  comments: any[];
  total: number;
  skip: number;
  limit: number;
}

/**
 * One thread page for the dialog, newest first, with the total beside it.
 *
 * The feed embeds only the opening window of each thread; this is what the "show
 * more" beneath it reads. Accepts both the new `{comments, total, skip, limit}`
 * envelope and a legacy bare array, normalising the latter to a single page.
 */
export const getConfessionComments = async (
  confessionId: string,
  params?: { skip?: number; limit?: number; sort?: 'latest' | 'relevant' },
): Promise<ConfessionCommentsPage> => {
  const response = await axios.get(`${API_URL}/confessions/${confessionId}/comments`, {
    headers: getAuthHeaders(),
    params: {
      ...(params?.skip !== undefined ? { skip: params.skip } : {}),
      ...(params?.limit !== undefined ? { limit: params.limit } : {}),
      ...(params?.sort ? { sort: params.sort } : {}),
    },
  });
  const data = response.data;
  if (Array.isArray(data)) {
    return { comments: data, total: data.length, skip: 0, limit: data.length };
  }
  return {
    comments: data?.comments ?? [],
    total: data?.total ?? (data?.comments?.length ?? 0),
    skip: data?.skip ?? 0,
    limit: data?.limit ?? (data?.comments?.length ?? 0),
  };
};

export const getConfessions = async (
  sortBy = 'time',
  params?: { skip?: number; limit?: number },
): Promise<ConfessionsPage> => {
  const response = await axios.get(`${API_URL}/confessions`, {
    headers: getAuthHeaders(),
    params: {
      sort_by: sortBy,
      ...(params?.skip !== undefined ? { skip: params.skip } : {}),
      ...(params?.limit !== undefined ? { limit: params.limit } : {}),
    },
  });
  const data = response.data;
  if (Array.isArray(data)) {
    return { confessions: data, total: data.length, skip: 0, limit: data.length };
  }
  return {
    confessions: data?.confessions ?? [],
    total: data?.total ?? (data?.confessions?.length ?? 0),
    skip: data?.skip ?? 0,
    limit: data?.limit ?? (data?.confessions?.length ?? 0),
  };
};

export const getTotalConfessionsCount = async () => {
    const response = await axios.get(`${API_URL}/confessions/total_count`, { headers: getAuthHeaders() });
    return response.data;
};

/**
 * Ask whether this account may post a confession right now.
 *
 * Advisory, and the server treats it as such: POST /confessions claims the window with a
 * conditional UPDATE and answers for itself, so this is only so the compose screen can show
 * the wait before anything is typed. `remaining_seconds` is here rather than something the
 * client parses out of `message` because a countdown cannot round-trip through prose.
 */
export const getConfessionCooldown = async () => {
  const response = await axios.get(`${API_URL}/confessions/cooldown`, { headers: getAuthHeaders() });
  return response.data as {
    status: string;
    message: string;
    cooldown_hours: number;
    remaining_minutes: number;
    remaining_seconds: number;
  };
};

export const createConfession = async (confession: any) => {
  const response = await axios.post(`${API_URL}/confessions`, confession, { headers: getAuthHeaders() });
  return response.data;
};

/**
 * Hybrid search over confessions (POST /confessions/search): keyword matches
 * first, similar-in-meaning below, in the existing public confession shape
 * (anonymous presentation preserved).
 *
 * `semantic.status` is `ok` when the embedding pipeline ran, otherwise the
 * reason the semantic half is empty: `disabled` | `unavailable` |
 * `rate_limited` | `skipped_short`. A non-`ok` status is a notice, not an
 * error: the keyword half still answers.
 */
export type HybridSemanticStatus =
  | 'ok'
  | 'disabled'
  | 'unavailable'
  | 'rate_limited'
  | 'skipped_short';

export interface HybridSearchResult {
  query: string;
  limit: number;
  keyword: { items: any[]; total: number };
  semantic: { items: any[]; status: HybridSemanticStatus };
}

/** Legacy alias for the semantic-only response era. */
export type SemanticSearchResult = HybridSearchResult;

export const searchConfessions = async (
  query: string,
  limit = 10,
  opts?: { signal?: AbortSignal },
): Promise<HybridSearchResult> => {
  const response = await axios.post(
    `${API_URL}/confessions/search`,
    { query, limit },
    { headers: getAuthHeaders(), ...(opts?.signal ? { signal: opts.signal } : {}) },
  );
  return response.data as HybridSearchResult;
};

/**
 * Staff hybrid search (GET /admin/confessions/search): the existing lexical
 * page as `keyword` plus a top-K semantic section with removed rows included
 * and flagged. Same `semantic.status` vocabulary as the public path.
 */
export interface AdminHybridSearchResult {
  query: string;
  filter: string;
  sort: string;
  skip: number;
  limit: number;
  keyword: { items: any[]; total: number; skip: number; limit: number };
  semantic: { items: any[]; status: HybridSemanticStatus };
}

export const searchAdminConfessions = async (
  params: { q: string; filter?: string; sort?: string; skip?: number; limit?: number },
  opts?: { signal?: AbortSignal },
): Promise<AdminHybridSearchResult> => {
  const response = await axios.get(`${API_URL}/admin/confessions/search`, {
    headers: getAuthHeaders(),
    params: {
      q: params.q,
      ...(params.filter && params.filter !== 'all' ? { filter: params.filter } : {}),
      ...(params.sort ? { sort: params.sort } : {}),
      ...(params.skip !== undefined ? { skip: params.skip } : {}),
      ...(params.limit !== undefined ? { limit: params.limit } : {}),
    },
    ...(opts?.signal ? { signal: opts.signal } : {}),
  });
  return response.data as AdminHybridSearchResult;
};

export const updateConfession = async (confessionId: string, updateData: { anonymous?: boolean; comments_enabled?: boolean }) => {
    const response = await axios.put(
      `${API_URL}/confessions/${confessionId}`,
      updateData,
      { headers: getAuthHeaders() }
    );
    return response.data;
};

export const reactToConfession = async (confessionId: string, reaction: string) => {
  const response = await axios.post(
    `${API_URL}/confessions/${confessionId}/react`,
    { reaction: reaction },
    { headers: getAuthHeaders() }
  );
  return response.data;
};

export const reportConfession = async (confessionId: string, reason: string) => {
    const response = await axios.post(
        `${API_URL}/confessions/${confessionId}/report`,
        { reason },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

// --- Comment Endpoints ---

export const createComment = async (confessionId: string, body: string) => {
  const response = await axios.post(
    `${API_URL}/confessions/${confessionId}/comment`,
    { body },
    { headers: getAuthHeaders() }
  );
  return response.data;
};

export const likeComment = async (commentId: string) => {
  const response = await axios.post(
    `${API_URL}/comments/${commentId}/like`,
    {},
    { headers: getAuthHeaders() }
  );
  return response.data;
};

export const dislikeComment = async (commentId: string) => {
  const response = await axios.post(
    `${API_URL}/comments/${commentId}/dislike`,
    {},
    { headers: getAuthHeaders() }
  );
  return response.data;
};

export const reportComment = async (commentId: string, reason: string) => {
    const response = await axios.post(
        `${API_URL}/comments/${commentId}/report`,
        { reason },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

// --- User & Auth Endpoints ---

export const getCurrentUser = async (token?: string) => {
  const headers = token
    ? { Authorization: `Bearer ${token}` }
    : getAuthHeaders();

  const response = await axios.get(`${API_URL}/auth/me`, {
    headers,
    // The same budget the refresh gets, and for the same reason: this read is
    // what the boot sequence waits on, and a backend that stops answering must
    // not hold the whole app on its splash screen. Without a budget axios waits
    // for ever, and there is no way out but a reload.
    signal: AbortSignal.timeout(SESSION_REQUEST_TIMEOUT_MS),
  });
  return response.data;
};

export const updateUserProfile = async (profileData: any) => {
    const response = await axios.put(`${API_URL}/profile/update`, profileData, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const uploadProfilePicture = async (file: File) => {
    const formData = new FormData();
    formData.append('file', file);

    // No manual Content-Type: axios sets multipart/form-data with the generated
    // boundary from the FormData. Naming the type ourselves sends the parts
    // without a boundary, which the server answers with a 400.
    const response = await axios.post(`${API_URL}/profile/upload-picture`, formData, {
        headers: {
            ...getAuthHeaders(),
        },
    });
    return response.data;
};

// --- Matchmaking Endpoints ---

export const checkMatchmakingStatus = async () => {
    const response = await axios.get(`${API_URL}/matchmaking/check`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const findMatch = async () => {
    const response = await axios.post(`${API_URL}/matchmaking/find`, {}, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

// --- Conversation Endpoints ---
export const requestConversation = async (match_id: string) => {
    console.log("API: requestConversation called with match_id:", match_id); // Debug log
    const response = await axios.post(`${API_URL}/conversations/request`, { match_id }, {
        headers: getAuthHeaders(),
    });
    console.log("API: requestConversation response:", response.data); // Debug log
    return response.data;
};

export const getConversationStatus = async (match_id: string) => {
    const response = await axios.get(`${API_URL}/conversations/${match_id}/status`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const acceptConversation = async (match_id: string) => {
    const response = await axios.post(`${API_URL}/conversations/${match_id}/accept`, {}, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const rejectConversation = async (match_id: string) => {
    const response = await axios.post(`${API_URL}/conversations/${match_id}/reject`, {}, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const sendMessage = async (conversation_id: string, body: string) => {
    const response = await axios.post(`${API_URL}/messages/send`, { conversation_id, body }, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const getCurrentConversation = async () => {
    const response = await axios.get(`${API_URL}/conversations/current`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const getReceivedConversations = async () => {
    const response = await axios.get(`${API_URL}/conversations/received`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const getConversationByMatch = async (match_id: string) => {
    const response = await axios.get(`${API_URL}/conversations/${match_id}`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const getConversationMessages = async (conversation_id: string) => {
    const response = await axios.get(`${API_URL}/messages/${conversation_id}`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const blockConversation = async (match_id: string) => {
    const response = await axios.post(`${API_URL}/conversations/${match_id}/block`, {}, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const unblockConversation = async (match_id: string) => {
    const response = await axios.post(`${API_URL}/conversations/${match_id}/unblock`, {}, {
        headers: getAuthHeaders(),
    });
    return response.data;
};


// --- Love Notes Endpoints ---

// One page of the love note recipient picker, with the total behind it.
//
// The page, the search and every filter are answered by the server -- SQL LIMIT/OFFSET plus
// WHERE clauses -- so the picker never downloads the participant table to filter it in the
// browser. Accepts both the `{users, total, skip, limit}` envelope and a legacy bare array
// (older servers and unit-test mocks), normalising the latter to a single page whose total
// is the array length.
export interface LoveNoteRecipientsPage {
    users: any[];
    total: number;
    skip: number;
    limit: number;
}

export const getLoveNoteRecipients = async (params?: {
    skip?: number;
    limit?: number;
    q?: string;
    includeSameGender?: boolean;
    year?: number;
    department?: string;
}): Promise<LoveNoteRecipientsPage> => {
    const response = await axios.get(`${API_URL}/love-notes/users`, {
        headers: getAuthHeaders(),
        params: {
            ...(params?.skip !== undefined ? { skip: params.skip } : {}),
            ...(params?.limit !== undefined ? { limit: params.limit } : {}),
            ...(params?.q ? { q: params.q } : {}),
            ...(params?.includeSameGender !== undefined
                ? { include_same_gender: params.includeSameGender }
                : {}),
            ...(params?.year !== undefined ? { year: params.year } : {}),
            ...(params?.department ? { department: params.department } : {}),
        },
    });
    const data = response.data;
    if (Array.isArray(data)) {
        return { users: data, total: data.length, skip: 0, limit: data.length };
    }
    const rows = data?.users ?? [];
    return {
        users: Array.isArray(rows) ? rows : [],
        total: data?.total ?? (Array.isArray(rows) ? rows.length : 0),
        skip: data?.skip ?? 0,
        limit: data?.limit ?? (Array.isArray(rows) ? rows.length : 0),
    };
};

/**
 * Every `(year, department)` pair at least one participant occupies, for the recipient filter.
 *
 * Renamed from `getClasses` along with the route it calls. The old name and the old shape -- a
 * list of one pre-joined string per participant -- described a single column that no longer
 * exists, and a filter over it could not ask "show me every 2nd yearer" without string matching.
 */
export const getCohorts = async (): Promise<Cohort[]> => {
    const response = await axios.get(`${API_URL}/love-notes/cohorts`, { headers: getAuthHeaders() });
    return response.data;
};

// Used to send a love note for review
export const sendLoveNote = async (noteData: {
    recipient_id: string;
    image_base64: string;
    body: string;
    anonymous: boolean;
}) => {
    const response = await axios.post(`${API_URL}/love-notes/send`, noteData, { headers: getAuthHeaders() });
    return response.data;
}

// One page of received love notes (accepted), newest first, with the total beside it.
//
// The page is a SQL LIMIT/OFFSET and the total a COUNT on the server, so a much-loved
// reader loads downwards instead of all at once -- which matters doubly here, because
// every row carries a freshly minted image URL. Accepts both the new
// `{notes, total, skip, limit}` envelope and a legacy bare array (older servers and
// unit-test mocks), normalising the latter to a single page whose total is the array
// length.
export interface ReceivedLoveNotesPage {
    notes: any[];
    total: number;
    skip: number;
    limit: number;
}

export const getReceivedLoveNotes = async (params?: {
    skip?: number;
    limit?: number;
}): Promise<ReceivedLoveNotesPage> => {
    const response = await axios.get(`${API_URL}/love-notes/inbox`, {
        headers: getAuthHeaders(),
        params: {
            ...(params?.skip !== undefined ? { skip: params.skip } : {}),
            ...(params?.limit !== undefined ? { limit: params.limit } : {}),
        },
    });
    const data = response.data;
    if (Array.isArray(data)) {
        return { notes: data, total: data.length, skip: 0, limit: data.length };
    }
    const rows = data?.notes ?? [];
    return {
        notes: Array.isArray(rows) ? rows : [],
        total: data?.total ?? (Array.isArray(rows) ? rows.length : 0),
        skip: data?.skip ?? 0,
        limit: data?.limit ?? (Array.isArray(rows) ? rows.length : 0),
    };
}

// Used to mark a love note as read
export const markLoveNoteRead = async (noteId: string) => {
    const response = await axios.post(`${API_URL}/love-notes/inbox/${noteId}/mark-read`, {}, { headers: getAuthHeaders() });
    return response.data;
}

// Used by the recipient to report a love note they received. The server refuses anyone who
// is not the recipient and anyone who has already filed, so the button is a request, not a
// decision: it reports that the note was reported.
export const reportLoveNote = async (noteId: string, reason: string) => {
    const response = await axios.post(
        `${API_URL}/love-notes/inbox/${noteId}/report`,
        { reason },
        { headers: getAuthHeaders() }
    );
    return response.data;
}

// --- Profile Endpoints ---

export interface NotificationsPage {
    notifications: any[];
    total: number;
    skip: number;
    limit: number;
}

/**
 * One page of the reader's notices, newest first, with the total beside it.
 *
 * The page is a SQL LIMIT/OFFSET and the total a COUNT on the server, so an account
 * with years of notices loads downwards instead of all at once. Accepts both the new
 * `{notifications, total, skip, limit}` envelope and a legacy bare array (older
 * servers and unit-test mocks), normalising the latter to a single page whose total
 * is the array length.
 */
export const getNotifications = async (params?: {
    skip?: number;
    limit?: number;
    q?: string;
}): Promise<NotificationsPage> => {
    const response = await axios.get(`${API_URL}/notifications`, {
        headers: getAuthHeaders(),
        params: {
            ...(params?.skip !== undefined ? { skip: params.skip } : {}),
            ...(params?.limit !== undefined ? { limit: params.limit } : {}),
            ...(params?.q ? { q: params.q } : {}),
        },
    });
    const data = response.data;
    if (Array.isArray(data)) {
        return { notifications: data, total: data.length, skip: 0, limit: data.length };
    }
    const rows = data?.notifications ?? [];
    return {
        notifications: Array.isArray(rows) ? rows : [],
        total: data?.total ?? (Array.isArray(rows) ? rows.length : 0),
        skip: data?.skip ?? 0,
        limit: data?.limit ?? (Array.isArray(rows) ? rows.length : 0),
    };
};

// --- Admin Endpoints ---

/**
 * The dashboard's figures, read once per load.
 *
 * `active_sessions` is a count of accounts and nothing more: the route that used to list each
 * one's last login, browser and IP is gone, because the "Active Today" card has no use for
 * that. `pending_report_queues` is the report backlog split by queue, next to its
 * `pending_reports` total, so the dashboard can say where the work is rather than only how much.
 */
export const getAdminStats = async () => {
    const response = await axios.get(`${API_URL}/admin/stats`, { headers: getAuthHeaders() });
    return response.data;
};

export interface AdminConfessionsQuery {
    skip?: number;
    limit?: number;
    q?: string;
    filter?: 'all' | 'reported' | 'anonymous' | 'with-comments';
    sort?: 'recent' | 'reports' | 'comments' | 'reactions';
}

export interface AdminConfessionsPage {
    confessions: any[];
    total: number;
    skip: number;
    limit: number;
}

/**
 * One page of confessions for the review screen, with every comment, report and
 * reactor row attached to the confessions on it.
 *
 * The search is a SQL WHERE across each confession and its sender, the filter picker a
 * SQL WHERE, the sort picker a SQL ORDER BY, and every page a SQL LIMIT/OFFSET on the
 * server, so a deployment with thousands of confessions returns at most one page per
 * request instead of the whole table. Accepts both the new
 * `{confessions, total, skip, limit}` envelope and a legacy bare array (older servers
 * and unit-test mocks), normalising the latter to a single page whose total is the
 * array length.
 */
export const getAdminConfessions = async (params?: AdminConfessionsQuery): Promise<AdminConfessionsPage> => {
    const response = await axios.get(`${API_URL}/admin/confessions`, {
        headers: getAuthHeaders(),
        params: {
            ...(params?.skip !== undefined ? { skip: params.skip } : {}),
            ...(params?.limit !== undefined ? { limit: params.limit } : {}),
            ...(params?.q ? { q: params.q } : {}),
            ...(params?.filter && params.filter !== 'all' ? { filter: params.filter } : {}),
            ...(params?.sort ? { sort: params.sort } : {}),
        },
    });
    const data = response.data;
    if (Array.isArray(data)) {
        return { confessions: data, total: data.length, skip: 0, limit: data.length };
    }
    return {
        confessions: data?.confessions ?? [],
        total: data?.total ?? (data?.confessions?.length ?? 0),
        skip: data?.skip ?? 0,
        limit: data?.limit ?? (data?.confessions?.length ?? 0),
    };
};

export const deleteAdminConfession = async (confessionId: string) => {
    const response = await axios.delete(`${API_URL}/admin/confessions/${confessionId}`, { headers: getAuthHeaders() });
    return response.data;
};

/**
 * Hide a confession from public view. One-way: there is no matching unhide call, and the
 * server has no route for one either.
 *
 * `already_hidden` is a separate flag from `content_removed` because the two answer different
 * questions -- the second is "did this click take something down", the first is "was it already
 * down" -- and a double click should not read to the reviewer as a second moderation action.
 */
export const hideAdminConfession = async (confessionId: string) => {
    const response = await axios.patch(
        `${API_URL}/admin/confessions/${confessionId}/hide`,
        {},
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/**
 * Hide one comment from its confession. The comment-level counterpart to
 * `hideAdminConfession`: same soft write, same one-way warning, same `already_hidden`
 * distinction between "this click took it down" and "it was already down".
 *
 * The parent confession is untouched, and its comment count moves with the removal, so the
 * feed stops advertising a comment nobody can open. The response carries `confession_id`
 * so the caller can re-sort or collapse the card it came from without a refetch.
 */
export const hideAdminComment = async (commentId: string) => {
    const response = await axios.patch(
        `${API_URL}/admin/comments/${commentId}/hide`,
        {},
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/**
 * Restore Azure-hidden content to public view. Only rows the system hid
 * (`removed_by_regno` marking Azure Content Safety) restore; manual hides
 * answer 409. Already-visible rows answer `already_visible`.
 */
export const unhideAdminConfession = async (confessionId: string) => {
    const response = await axios.patch(
        `${API_URL}/admin/confessions/${confessionId}/unhide`,
        {},
        { headers: getAuthHeaders() }
    );
    return response.data;
};

export const unhideAdminComment = async (commentId: string) => {
    const response = await axios.patch(
        `${API_URL}/admin/comments/${commentId}/unhide`,
        {},
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/**
 * Restore an Azure-hidden chat message to both participants. Same gate as the
 * confession/comment restores: only rows the system hid restore, manual hides
 * answer 409, and already-visible rows answer `already_visible`.
 *
 * `kind` picks the table: `matchmaking` (the default, so every existing caller
 * keeps working) or `case_closed` for a detective-thread turn.
 */
export const unhideAdminMessage = async (
    messageId: string,
    kind: 'matchmaking' | 'case_closed' = 'matchmaking'
) => {
    const response = await axios.patch(
        `${API_URL}/admin/messages/${messageId}/unhide`,
        {},
        { headers: getAuthHeaders(), params: { kind } }
    );
    return response.data;
};

export interface AdminLoveNotesQuery {
    skip?: number;
    limit?: number;
    status?: 'all' | 'pending_review' | 'approved' | 'rejected';
    order?: 'newest' | 'oldest';
}

export interface AdminLoveNotesPage {
    notes: any[];
    total: number;
    skip: number;
    limit: number;
}

/**
 * One page of love notes for the moderation screen.
 *
 * The status filter is a SQL WHERE, the timeline order a SQL ORDER BY, and every page a
 * SQL LIMIT/OFFSET on the server, so a deployment with thousands of notes returns at most
 * one page per request instead of the whole table. Accepts both the new
 * `{notes, total, skip, limit}` envelope and a legacy bare array (older servers and
 * unit-test mocks), normalising the latter to a single page whose total is the array
 * length.
 */
export const getAdminLoveNotes = async (params?: AdminLoveNotesQuery): Promise<AdminLoveNotesPage> => {
    const response = await axios.get(`${API_URL}/admin/love-notes`, {
        headers: getAuthHeaders(),
        params: {
            ...(params?.skip !== undefined ? { skip: params.skip } : {}),
            ...(params?.limit !== undefined ? { limit: params.limit } : {}),
            ...(params?.status && params.status !== 'all' ? { status: params.status } : {}),
            ...(params?.order ? { order: params.order } : {}),
        },
    });
    const data = response.data;
    if (Array.isArray(data)) {
        return { notes: data, total: data.length, skip: 0, limit: data.length };
    }
    return {
        notes: data?.notes ?? [],
        total: data?.total ?? (data?.notes?.length ?? 0),
        skip: data?.skip ?? 0,
        limit: data?.limit ?? (data?.notes?.length ?? 0),
    };
};

export const updateAdminLoveNoteStatus = async (noteId: string, status: "approved" | "rejected" | "pending_review") => {
    const response = await axios.put(
        `${API_URL}/admin/love-notes/${noteId}/status`,
        {},
        {
            headers: getAuthHeaders(),
            params: { status },
        }
    );
    return response.data;
};

/**
 * Re-run PII redaction for one love note whose moderation preview is missing.
 *
 * The note was accepted even though the provider failed at send time, so the preview
 * column is empty and the moderation screen shows an explicit unavailable state. This
 * asks the backend to try again and answers `{ id, moderation_body }` -- the redacted
 * preview, never the original note text, which has no field in any response. A note
 * that already has a preview comes back unchanged without a provider call; a provider
 * failure is a 503 and the caller keeps the unavailable state.
 */
export const retryAdminLoveNoteRedaction = async (
    noteId: string
): Promise<{ id: string; moderation_body: string }> => {
    const response = await axios.post(
        `${API_URL}/admin/love-notes/${noteId}/redact`,
        {},
        { headers: getAuthHeaders() }
    );
    return response.data;
};

export const deleteAdminLoveNote = async (noteId: string) => {
    const response = await axios.delete(`${API_URL}/admin/love-notes/${noteId}`, { headers: getAuthHeaders() });
    return response.data;
};

/**
 * The query shape all three report queues share.
 *
 * The status filter is a SQL WHERE, the search a SQL match on the reason, the reported
 * content and the parties named on the row, and every page a SQL LIMIT/OFFSET -- so a
 * queue with thousands of reports returns at most one page per request, with the total
 * counted under the same filter beside it. Each getter below keeps its own literal URL
 * (rather than sharing one helper) because the route-coverage test reads them out of
 * this file by pattern.
 */
export interface AdminReportQueueQuery {
    status?: string;
    limit?: number;
    skip?: number;
    q?: string;
}

const reportQueueParams = (params?: AdminReportQueueQuery) => ({
    ...(params?.status ? { status: params.status } : {}),
    ...(params?.limit !== undefined ? { limit: params.limit } : {}),
    ...(params?.skip !== undefined ? { skip: params.skip } : {}),
    ...(params?.q ? { q: params.q } : {}),
});

export const getAdminLoveNoteReports = async (params?: AdminReportQueueQuery) => {
    const response = await axios.get(`${API_URL}/admin/love-notes/reports`, {
        headers: getAuthHeaders(),
        params: reportQueueParams(params),
    });
    return response.data;
};

/** How a moderator can close a report, as the three queues accept it.
 *
 * `removed` is the soft removal: the content leaves every participant-facing read but stays
 * in the database, and both the author and the reporter are notified. `dismissed` leaves the
 * content alone and notifies only the reporter. `reviewed` is the older word for `dismissed`
 * and is still accepted so a stale caller is not met with a 422; nothing in the UI sends it.
 */
export type AdminReportResolution = "removed" | "dismissed" | "reviewed";

/** { id, status, content_removed } -- whether this call is what took the content down. */
export interface AdminReportResolutionResult {
    id: string;
    status: AdminReportResolution;
    content_removed: boolean;
}

export const updateAdminLoveNoteReportStatus = async (
    reportId: string,
    status: AdminReportResolution
): Promise<AdminReportResolutionResult> => {
    const response = await axios.patch(
        `${API_URL}/admin/love-notes/reports/${reportId}`,
        {},
        { headers: getAuthHeaders(), params: { status } }
    );
    return response.data;
};

// The three report queues, read together on the Reports Review screen. They are three
// endpoints rather than one aggregate call because they answer three different questions --
// what did someone report a post for, a love note for, or a chat message for -- and a single
// endpoint over three tables would have to invent one row shape for three payloads that have
// nothing in common but a reason and a status. Every one of them takes the same
// `AdminReportQueueQuery` and answers with the same { reports, total, skip, limit }
// envelope, so the screen can treat them uniformly without the server pretending they
// are the same data.
//
// `limit` is for a caller that wants `total` and nothing else -- the moderator dashboard's
// count cards ask for one row and read the envelope's count, rather than downloading fifty
// reported messages to add up a number the query already returned. It is a server-side filter
// and not a display length, so `total` still counts every matching row however few came back.

export const getAdminConfessionReports = async (params?: AdminReportQueueQuery) => {
    const response = await axios.get(`${API_URL}/admin/confessions/reports`, {
        headers: getAuthHeaders(),
        params: reportQueueParams(params),
    });
    return response.data;
};

export const updateAdminConfessionReportStatus = async (
    reportId: string,
    status: AdminReportResolution
): Promise<AdminReportResolutionResult> => {
    const response = await axios.patch(
        `${API_URL}/admin/confessions/reports/${reportId}`,
        {},
        { headers: getAuthHeaders(), params: { status } }
    );
    return response.data;
};

export const getAdminMessageReports = async (params?: AdminReportQueueQuery) => {
    const response = await axios.get(`${API_URL}/admin/messages/reports`, {
        headers: getAuthHeaders(),
        params: reportQueueParams(params),
    });
    return response.data;
};

export const updateAdminMessageReportStatus = async (
    reportId: string,
    status: AdminReportResolution,
    kind: 'matchmaking' | 'case_closed' = 'matchmaking'
): Promise<AdminReportResolutionResult> => {
    const response = await axios.patch(
        `${API_URL}/admin/messages/reports/${reportId}`,
        {},
        { headers: getAuthHeaders(), params: { status, kind } }
    );
    return response.data;
};

export interface AdminUsersQuery {
    skip?: number;
    limit?: number;
    q?: string;
    account_type?: AccountType | string;
    year?: number;
    department?: string;
    sort?: string;
    flagged?: 'all' | 'flagged' | 'ordinary';
}

export interface AdminUsersPage {
    users: any[];
    total: number;
    skip: number;
    limit: number;
}

/**
 * One page of accounts for the profile-review screen.
 *
 * Every filter is a SQL WHERE and every page is a SQL LIMIT/OFFSET on the server, so a
 * deployment with ten thousand participants returns at most one page per request instead
 * of the whole table. Accepts both the new `{users, total, skip, limit}` envelope and a
 * legacy bare array (older servers and unit-test mocks), normalising the latter to a
 * single page whose total is the array length.
 */
export const getAdminUsers = async (params?: AdminUsersQuery): Promise<AdminUsersPage> => {
    const response = await axios.get(`${API_URL}/admin/users`, {
        headers: getAuthHeaders(),
        params: {
            ...(params?.skip !== undefined ? { skip: params.skip } : {}),
            ...(params?.limit !== undefined ? { limit: params.limit } : {}),
            ...(params?.q ? { q: params.q } : {}),
            ...(params?.account_type ? { account_type: params.account_type } : {}),
            ...(params?.year !== undefined && params.year !== null ? { year: params.year } : {}),
            ...(params?.department ? { department: params.department } : {}),
            ...(params?.sort ? { sort: params.sort } : {}),
            ...(params?.flagged && params.flagged !== 'all' ? { flagged: params.flagged } : {}),
        },
    });
    const data = response.data;
    if (Array.isArray(data)) {
        return { users: data, total: data.length, skip: 0, limit: data.length };
    }
    return {
        users: data?.users ?? [],
        total: data?.total ?? (data?.users?.length ?? 0),
        skip: data?.skip ?? 0,
        limit: data?.limit ?? (data?.users?.length ?? 0),
    };
};

/**
 * The fields `POST /admin/users` accepts.
 *
 * An admin mints an identity and nothing else: regno, name, email, and for a participant the
 * gender, year and department that place them in a cohort. Every field the account owns about
 * itself -- picture, bio, interests, and the four preference flags -- is absent, and the
 * server forbids those names outright rather than ignoring them, so a payload carrying one is a
 * 422 and not a silent no-op. There is nothing to upload here either: a participant sets their
 * own picture through their profile once the account exists.
 *
 * `email` is not optional because the server refuses a payload without it on any account type,
 * and a type that said otherwise would let a dialog submit one.
 *
 * `gender`, `year` and `department` are required of a participant and omitted for staff. Gender
 * used to be listed with `email` as required of every account; the staff tables no longer carry
 * the column, so a staff create that sent one was writing a value the router drops.
 *
 * `year` and `department` are two fields rather than one because the server stores two fields:
 * a payload joining them into a single `class_name`-shaped string would have nowhere to go,
 * and would re-create the ambiguity the split exists to remove.
 */
export interface AdminUserCreatePayload {
    account_type: AccountType;
    regno: string;
    name: string;
    email: string;
    /** Participants only. No `gender` column exists on `admin` or `moderator`. */
    gender?: UserGender;
    /** Participants only. Accepted but not written for staff, which has no such columns. */
    year?: UserYear;
    department?: string;
}

/**
 * The fields `PUT /admin/users/{id}` accepts. All optional.
 *
 * The same identity set as the create payload, minus `account_type` (which table an account
 * lives in is fixed at creation) and minus every field the account owns itself. Omitting a
 * field leaves it alone; there is no longer any list that `[]` could clear.
 *
 * `gender`, `year` and `department` are participant-only: the server drops those three keys
 * from an update aimed at a staff row, so a client should not send them for one either --
 * the value would vanish on the next fetch and look like an edit that did not stick.
 */
export interface AdminUserUpdatePayload {
    regno?: string;
    name?: string;
    email?: string;
    gender?: UserGender;
    year?: UserYear;
    department?: string;
}

export const createAdminUser = async (payload: AdminUserCreatePayload) => {
    const response = await axios.post(
        `${API_URL}/admin/users`,
        payload,
        { headers: getAuthHeaders() }
    );
    return response.data;
};

export const setAdminUserBlocked = async (userId: string, blocked: boolean) => {
    const response = await axios.put(
        `${API_URL}/admin/users/${userId}/block`,
        {},
        {
            headers: getAuthHeaders(),
            params: { blocked },
        }
    );
    return response.data;
};

export const updateAdminUser = async (userId: string, payload: AdminUserUpdatePayload) => {
    const response = await axios.put(
        `${API_URL}/admin/users/${userId}`,
        payload,
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/**
 * The result of one all-or-nothing participant upload.
 *
 * `created` is how many rows were written; there is no partial result by construction.
 * `total_users`, `max_user_count` and `remaining_slots` are included so the screen can say
 * what the upload did to the account ceiling without a second statistics read.
 */
export interface AdminBulkUploadResult {
    created: number;
    total_users: number;
    max_user_count: number;
    remaining_slots: number;
}

/**
 * Upload a CSV or XLSX file of participant accounts.
 *
 * Participants only: the server's file format has no account-type column and this helper
 * sends none. The entire file is validated and applied in one transaction, so a rejection
 * means nothing was written rather than "some rows failed". The request is multipart, and
 * the browser's boundary is left for Axios to set; setting `Content-Type` manually here
 * would omit it and make the server unable to parse the form.
 */
export const bulkUploadAdminUsers = async (file: File): Promise<AdminBulkUploadResult> => {
    const form = new FormData();
    form.append('file', file);
    const response = await axios.post(`${API_URL}/admin/users/bulk-upload`, form, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/**
 * One configured Wordle puzzle, as `GET /admin/wordle` returns it.
 *
 * `answer` is present deliberately: this is the administrator's own configuration screen, and
 * the endpoint it comes from is closed to everyone who may not publish it. There is no
 * participant-facing read of this shape.
 */
export interface WordleGame {
    id: string;
    game_date: string;
    mode: 'custom' | 'random';
    /** Null while the session is pending (manual mode, word not yet provided). */
    answer: string | null;
    /** Lifecycle state: active, pending or ended. */
    status?: string;
    period_start_date?: string | null;
    period_end_date?: string | null;
    created_at: string;
    updated_at: string;
}

/** A word the server drew, with the signature that binds it to one day. */
export interface WordleDraw {
    word: string;
    token: string;
}

/**
 * The fields `POST /admin/wordle` accepts.
 *
 * `word` and `token` are both optional here and required by the *mode*: a custom game needs a
 * word from the approved list, a random one needs the word a draw returned together with that
 * draw's token. The server re-derives the signature, so a token minted for another day -- or
 * one a client made up -- is a 400 rather than a second chance to choose the answer.
 *
 * `game_date` identifies the puzzle and defaults to today in UTC when omitted. It is sent on
 * every save from the console rather than left to the default, so the day a draw was made for
 * and the day the answer lands on are visibly the same field.
 */
export interface WordleGamePayload {
    mode: 'custom' | 'random';
    word?: string;
    token?: string;
    game_date?: string;
}

/** Draw an answer for `gameDate` (defaults to today in UTC). Read-only: nothing is stored. */
export const drawWordleAnswer = async (gameDate?: string): Promise<WordleDraw> => {
    const response = await axios.get(`${API_URL}/admin/wordle/preview`, {
        headers: getAuthHeaders(),
        params: { game_date: gameDate },
    });
    return response.data;
};

/** Configure one day's puzzle. A day that already has one answers 409. */
export const createWordleGame = async (payload: WordleGamePayload): Promise<WordleGame> => {
    const response = await axios.post(`${API_URL}/admin/wordle`, payload, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/**
 * One page of every configured puzzle, newest day first, with the total beside it.
 *
 * The page is a SQL LIMIT/OFFSET and the total a COUNT on the server, so a season of
 * daily sessions loads downwards instead of all at once. Accepts both the new
 * `{games, total, skip, limit}` envelope and a legacy bare array (older servers and
 * unit-test mocks), normalising the latter to a single page whose total is the array
 * length.
 */
export interface AdminGamesPage<T> {
    games: T[];
    total: number;
    skip: number;
    limit: number;
}

const normalizeGamesPage = <T,>(data: unknown, key: string): { rows: T[]; total: number } => {
    if (Array.isArray(data)) {
        return { rows: data as T[], total: data.length };
    }
    const rows = (data as Record<string, unknown> | null | undefined)?.[key];
    return {
        rows: Array.isArray(rows) ? (rows as T[]) : [],
        total:
            typeof (data as Record<string, unknown> | null | undefined)?.total === 'number'
                ? (data as { total: number }).total
                : Array.isArray(rows)
                  ? rows.length
                  : 0,
    };
};

export const getAdminWordleGames = async (params?: {
    skip?: number;
    limit?: number;
}): Promise<AdminGamesPage<WordleGame>> => {
    const response = await axios.get(`${API_URL}/admin/wordle`, {
        headers: getAuthHeaders(),
        params: {
            ...(params?.skip !== undefined ? { skip: params.skip } : {}),
            ...(params?.limit !== undefined ? { limit: params.limit } : {}),
        },
    });
    const { rows, total } = normalizeGamesPage<WordleGame>(response.data, 'games');
    return { games: rows, total, skip: params?.skip ?? 0, limit: params?.limit ?? rows.length };
};

/** One configured puzzle by id. A malformed id answers 400, an unknown one 404. */
export const getAdminWordleGame = async (gameId: string): Promise<WordleGame> => {
    const response = await axios.get(`${API_URL}/admin/wordle/${gameId}`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

// --- Anagram Configuration Endpoints ---

/**
 * One configured Anagram puzzle, as `GET /admin/anagram` returns it.
 *
 * `answer` is the canonical, uppercase word and never a scramble: each player's own
 * arrangement of it is generated per user and does not exist yet, so there is nothing
 * scrambled to show. `created_by` is the publishing account's regno, so the list can say who
 * set each day's word.
 */
export interface AnagramGame {
    id: string;
    game_date: string;
    mode: 'custom' | 'random';
    /** Null while the session is pending (manual mode, word not yet provided). */
    answer: string | null;
    /** Lifecycle state: active, pending or ended. */
    status?: string;
    period_start_date?: string | null;
    period_end_date?: string | null;
    created_by: string;
    created_at: string;
    updated_at: string;
}

/**
 * The fields `POST /admin/anagram` accepts.
 *
 * `word` and `token` are both optional here and required by the *mode*: `custom` needs a
 * word, `random` needs the word a preview drew together with that draw's token. The server
 * re-derives the signature, so a token minted for another day -- or one a client made up --
 * is a 400 rather than a second chance to choose the answer.
 *
 * `game_date` identifies the puzzle and defaults to today in UTC when omitted, so a future
 * day can be scheduled and a past day corrected.
 */
export interface AnagramGamePayload {
    mode: 'custom' | 'random';
    word?: string;
    token?: string;
    game_date?: string;
}

/** A word the server drew, with the signature that binds it to one day. */
export interface AnagramDraw {
    word: string;
    token: string;
}

/** Draw a word for `gameDate` (defaults to today in UTC). Read-only: nothing is stored. */
export const drawAnagramAnswer = async (gameDate?: string): Promise<AnagramDraw> => {
    const response = await axios.get(`${API_URL}/admin/anagram/preview`, {
        headers: getAuthHeaders(),
        params: { game_date: gameDate },
    });
    return response.data;
};

/** Configure one day's puzzle. A day that already has one answers 409. */
export const createAnagramGame = async (payload: AnagramGamePayload): Promise<AnagramGame> => {
    const response = await axios.post(`${API_URL}/admin/anagram`, payload, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** One page of every configured puzzle, newest day first, with the total beside it. */
export const getAdminAnagramGames = async (params?: {
    skip?: number;
    limit?: number;
}): Promise<AdminGamesPage<AnagramGame>> => {
    const response = await axios.get(`${API_URL}/admin/anagram`, {
        headers: getAuthHeaders(),
        params: {
            ...(params?.skip !== undefined ? { skip: params.skip } : {}),
            ...(params?.limit !== undefined ? { limit: params.limit } : {}),
        },
    });
    const { rows, total } = normalizeGamesPage<AnagramGame>(response.data, 'games');
    return { games: rows, total, skip: params?.skip ?? 0, limit: params?.limit ?? rows.length };
};

/** One configured puzzle by id. A malformed id answers 400, an unknown one 404. */
export const getAdminAnagramGame = async (gameId: string): Promise<AnagramGame> => {
    const response = await axios.get(`${API_URL}/admin/anagram/${gameId}`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

// --- Couple Configuration Endpoints ---

/**
 * One featured Couple Prediction game, as `GET /admin/couple` returns it.
 *
 * There is deliberately no mode, word or answer here: featuring a day stores the
 * day and who featured it, and nothing else. The 50-question bank is shared
 * application content seeded at startup, never per-day input, so there is no
 * puzzle to preview and no content to select.
 */
export interface CoupleGame {
    id: string;
    game_date: string;
    created_by: string;
    created_at: string;
    updated_at: string;
}

/**
 * The fields `POST /admin/couple` accepts.
 *
 * Only the day. It identifies the featured instance and defaults to today in UTC
 * when omitted, so a future day can be scheduled and a past day corrected.
 */
export interface CoupleGamePayload {
    game_date?: string;
}

/** Mark the game featured for one day. A day that is already featured answers 409. */
export const createCoupleGame = async (payload: CoupleGamePayload): Promise<CoupleGame> => {
    const response = await axios.post(`${API_URL}/admin/couple`, payload, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Every featured date, newest day first. */
export const getAdminCoupleGames = async (): Promise<CoupleGame[]> => {
    const response = await axios.get(`${API_URL}/admin/couple`, { headers: getAuthHeaders() });
    return response.data;
};

/** One featured date by id. A malformed id answers 400, an unknown one 404. */
export const getAdminCoupleGame = async (gameId: string): Promise<CoupleGame> => {
    const response = await axios.get(`${API_URL}/admin/couple/${gameId}`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

// --- Wordle Gameplay Endpoints ---

/** The colour verdict a position in a guessed word earned. */
export type WordleTile = 'absent' | 'present' | 'correct';

/** How a participation stands: in progress, or finished as a win or a loss. */
export type WordleStatus = 'in_progress' | 'won' | 'lost';

/**
 * One player's place in one day's puzzle, as the wordle screen renders it.
 *
 * ``guesses`` is the played history in order and ``results`` its per-letter verdicts in the
 * same order and at the same length. ``answer`` is the reveal and is null while the game is
 * still in progress -- a completed puzzle has nothing left to hide, and the answer is the one
 * field a player who lost the game is owed.
 */
export interface WordleParticipation {
    wordle_game_id: string;
    game_date: string;
    status: WordleStatus;
    success: boolean;
    attempt_count: number;
    max_attempts: number;
    guesses: string[];
    results: WordleTile[][];
    started_at: string;
    last_guess_at: string | null;
    completed_at: string | null;
    solved_at: string | null;
    answer: string | null;
}

/**
 * Today's puzzle and this player's participation, as the game screen opens.
 *
 * ``game_id`` and ``game_date`` are null when nothing is playable yet; ``participation`` is
 * null until the player commits a first guess, because reading the board must not create a
 * row. ``max_attempts`` and ``word_length`` let the screen draw its empty board without a
 * second round trip.
 */
export interface WordleCurrentGame {
    game_id: string | null;
    game_date: string | null;
    participation: WordleParticipation | null;
    /** Mirrors the session lifecycle; `pending` means the word is not set yet. */
    session_status?: string | null;
    /** Sentence the screen shows when the session is pending. */
    message?: string | null;
    max_attempts: number;
    word_length: number;
}

/** A participation after one guess, plus the verdict that guess earned. */
export interface WordleGuessResponse extends WordleParticipation {
    guess: string;
    result: WordleTile[];
}

/** Today's playable puzzle and this player's participation. Never creates a row. */
export const getCurrentWordle = async (): Promise<WordleCurrentGame> => {
    const response = await axios.get(`${API_URL}/games/wordle`, { headers: getAuthHeaders() });
    return response.data;
};

/** Score one guess against one puzzle and store it for the current player. */
export const submitWordleGuess = async (
    gameId: string,
    guess: string
): Promise<WordleGuessResponse> => {
    const response = await axios.post(
        `${API_URL}/games/wordle/guess`,
        { game_id: gameId, guess },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

// --- Anagram Gameplay Endpoints ---

/** Whether a participation is still being played or has been settled. */
export type AnagramStatus = 'in_progress' | 'won' | 'lost';

/**
 * One player's place in one day's puzzle: their scramble, guesses, attempts and time.
 *
 * `scrambled_word` is the private permutation dealt at reveal -- the tiles on screen.
 * `active_time_seconds` banks finished timing segments; `timer_started_at` opens the
 * current one and is null while paused or finished. `timer_running` restates "an open,
 * non-stale segment on a game still in progress" so the screen knows whether to tick or
 * offer RESUME. `answer` is null until the game is over, and then only a loss needs it
 * shown -- a winner already typed it.
 */
export interface AnagramParticipation {
    anagram_game_id: string;
    game_date: string;
    status: AnagramStatus;
    success: boolean;
    attempt_count: number;
    max_attempts: number;
    guesses: string[];
    scrambled_word: string;
    active_time_seconds: number;
    timer_started_at: string | null;
    timer_running: boolean;
    started_at: string;
    last_guess_at: string | null;
    completed_at: string | null;
    solved_at: string | null;
    answer: string | null;
}

/**
 * Today's puzzle and this player's participation, as the game screen opens.
 *
 * `game_id` and `game_date` are null when nothing is playable yet; `participation` is
 * null until the player presses REVEAL WORD, because reading the screen must not create a
 * row. `max_attempts` lets the screen draw its attempts line without a second round trip.
 */
export interface AnagramCurrentGame {
    game_id: string | null;
    game_date: string | null;
    participation: AnagramParticipation | null;
    /** Mirrors the session lifecycle; `pending` means the word is not set yet. */
    session_status?: string | null;
    /** Sentence the screen shows when the session is pending. */
    message?: string | null;
    max_attempts: number;
}

/** A participation after one guess, plus whether it matched. */
export interface AnagramGuessResponse extends AnagramParticipation {
    guess: string;
    correct: boolean;
}

/** Today's playable puzzle and this player's participation. Never creates a row. */
export const getCurrentAnagram = async (): Promise<AnagramCurrentGame> => {
    const response = await axios.get(`${API_URL}/games/anagram`, { headers: getAuthHeaders() });
    return response.data;
};

/**
 * Reveal the puzzle: deal this player's scramble and start -- or resume -- the clock.
 * Idempotent: a later call on a paused game re-opens the clock, on a running game reads
 * it back unchanged.
 */
export const startAnagram = async (gameId: string): Promise<AnagramParticipation> => {
    const response = await axios.post(
        `${API_URL}/games/anagram/start`,
        { game_id: gameId },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Bank the open timing segment and stop the clock. Idempotent. */
export const pauseAnagram = async (gameId: string): Promise<AnagramParticipation> => {
    const response = await axios.post(
        `${API_URL}/games/anagram/pause`,
        { game_id: gameId },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/**
 * Best-effort pause for page teardown (`pagehide`, abrupt unmount).
 *
 * A normal `pauseAnagram` call may never get its response once the page is going away, so
 * this uses `fetch` with `keepalive` instead: the browser delivers the request even while
 * unloading. Fire-and-forget by design -- the response is unreadable here, and a pause
 * that never lands is bounded server-side by the stale-segment rule rather than by a
 * retry this context cannot run.
 */
export const pauseAnagramBeacon = (gameId: string): void => {
    try {
        const token = getAccessToken();
        if (!token) return;
        void fetch(`${API_URL}/games/anagram/pause`, {
            method: 'POST',
            headers: {
                Authorization: `Bearer ${token}`,
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ game_id: gameId }),
            keepalive: true,
        });
    } catch {
        // Unloading: nothing left to report the failure to.
    }
};

/** Checkpoint a running clock without stopping it. A no-op read when paused. */
export const heartbeatAnagram = async (gameId: string): Promise<AnagramParticipation> => {
    const response = await axios.post(
        `${API_URL}/games/anagram/heartbeat`,
        { game_id: gameId },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Score one tile arrangement against one puzzle and store it for the current player. */
export const submitAnagramGuess = async (
    gameId: string,
    guess: string
): Promise<AnagramGuessResponse> => {
    const response = await axios.post(
        `${API_URL}/games/anagram/guess`,
        { game_id: gameId, guess },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

// --- Couple Questionnaire Gameplay Endpoints ---

/** Where the caller stands in the featured Couple Questionnaire. */
export type CoupleStage =
    | 'no_game'
    | 'selecting_partner'
    | 'answering'
    | 'waiting_partner'
    | 'prediction'
    | 'completed';

/** One user the caller may choose as a partner. */
export interface CouplePartnerCandidate {
    id: string;
    name: string;
    gender: string;
    year: number;
    department: string;
}

/**
 * One page of partner candidates, with the total behind the current filters.
 *
 * The picker pages, searches and filters in SQL: `total` is what tells the
 * screen how many candidates the filters match so "load more" can say what
 * remains and stop at the end. The backend reads and serialises at most
 * `limit` rows per answer however big the participant table gets.
 */
export interface CouplePartnersPage {
    partners: CouplePartnerCandidate[];
    total: number;
    skip: number;
    limit: number;
}

/** The SQL-side filters the partner picker may send. */
export interface CouplePartnersQuery {
    skip?: number;
    limit?: number;
    /** Case-insensitive substring match on the name, run in SQL. */
    q?: string;
    /** 'different' hides the caller's own gender (the default); 'same' shows only it. */
    genderMode?: 'different' | 'same';
    /** Half of the cohort filter: the year in "CSE 2". */
    year?: number;
    /** The other half: the department in "CSE 2", matched case-insensitively. */
    department?: string;
}

/**
 * One page from one game's partner endpoint.
 *
 * Accepts both the `{partners, total, skip, limit}` envelope and a legacy bare array
 * (older servers and unit-test mocks), normalising the latter to a single page whose
 * total is the array length. The rows themselves are `CouplePartnerCandidate`s.
 */
const fetchCouplePartners = async (
    path: string,
    params?: CouplePartnersQuery
): Promise<CouplePartnersPage> => {
    const response = await axios.get(`${API_URL}${path}`, {
        headers: getAuthHeaders(),
        params: {
            ...(params?.skip !== undefined ? { skip: params.skip } : {}),
            ...(params?.limit !== undefined ? { limit: params.limit } : {}),
            ...(params?.q ? { q: params.q } : {}),
            ...(params?.genderMode ? { gender_mode: params.genderMode } : {}),
            ...(params?.year !== undefined ? { year: params.year } : {}),
            ...(params?.department ? { department: params.department } : {}),
        },
    });
    const data = response.data;
    if (Array.isArray(data)) {
        return { partners: data, total: data.length, skip: 0, limit: data.length };
    }
    const rows = data?.partners ?? [];
    return {
        partners: Array.isArray(rows) ? rows : [],
        total: data?.total ?? (Array.isArray(rows) ? rows.length : 0),
        skip: data?.skip ?? 0,
        limit: data?.limit ?? (Array.isArray(rows) ? rows.length : 0),
    };
};

/**
 * Every `(year, department)` pair a partner candidate can belong to, for the pickers' filter.
 *
 * One route serves all four couple games: they pair from the same participant table, so
 * they are offered the same palette of cohorts that actually exist -- the picker's single
 * "CSE 1" option means the year and the department at once, and both halves come from the
 * rows themselves rather than a hand-typed list that can drift.
 */
export const getPartnerCohorts = async (): Promise<Cohort[]> => {
    const response = await axios.get(`${API_URL}/games/partner-cohorts`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** One of the caller's own questions, with the bank's `X` rendered as "you". */
export interface CoupleQuestionView {
    session_question_id: string;
    question_id: string;
    question_text: string;
    options: string[];
    shared: boolean;
    selected_option_index: number | null;
}

/** One of the partner's questions, with `X` rendered as the partner's name. */
export interface CouplePredictionView {
    session_question_id: string;
    question_id: string;
    question_text: string;
    options: string[];
    shared: boolean;
    predicted_option_index: number | null;
}

/** One settled prediction, visible only in the completed result. */
export interface CoupleReviewItem {
    session_question_id: string;
    question_id: string;
    question_text: string;
    options: string[];
    shared: boolean;
    original_option_index: number;
    predicted_option_index: number;
    correct: boolean;
}

/** The settled score, derived server-side from stored answers and predictions. */
export interface CoupleResultView {
    my_score: number;
    partner_score: number;
    max_score: number;
    my_correct_count: number;
    my_prediction_count: number;
    partner_correct_count: number;
    partner_prediction_count: number;
    completed_at: string | null;
    review: CoupleReviewItem[];
}

/**
 * The caller's place in the featured game, as the page opens and after every
 * write. `questions` holds the caller's own fifteen while answering,
 * `predictions` the partner's fifteen while predicting, and `result` the
 * settled score once both sides are done -- each present only in its phase.
 */
export interface CoupleCurrentState {
    stage: CoupleStage;
    game_id: string | null;
    game_date: string | null;
    my_selection: { selected_user_id: string; selected_user_name: string } | null;
    session_id: string | null;
    partner: { id: string; name: string } | null;
    my_answers_submitted: number;
    my_answers_total: number;
    partner_answers_submitted: number;
    partner_answers_total: number;
    my_predictions_submitted: number;
    my_predictions_total: number;
    waiting_for: 'answers' | 'predictions' | null;
    questions: CoupleQuestionView[];
    predictions: CouplePredictionView[];
    result: CoupleResultView | null;
}

/** The caller's current state. Never creates anything. */
export const getCoupleState = async (): Promise<CoupleCurrentState> => {
    const response = await axios.get(`${API_URL}/games/couple-questionnaire`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** One page of participants the caller may choose, excluding only themselves. */
export const getCouplePartners = (
    params?: CouplePartnersQuery
): Promise<CouplePartnersPage> =>
    fetchCouplePartners('/games/couple-questionnaire/partners', params);

/**
 * Record the caller's partner choice. A changed mind replaces the previous
 * pick; a mutual pick creates the one session for the pair. Answers the new
 * state, so the screen advances in one round trip.
 */
export const selectCouplePartner = async (
    selectedUserId: string
): Promise<CoupleCurrentState> => {
    const response = await axios.post(
        `${API_URL}/games/couple-questionnaire/selection`,
        { selected_user_id: selectedUserId },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Store one of the caller's own answers. Insert-only: answering twice is refused. */
export const submitCoupleAnswer = async (
    sessionQuestionId: string,
    selectedOptionIndex: number
): Promise<CoupleCurrentState> => {
    const response = await axios.post(
        `${API_URL}/games/couple-questionnaire/answers`,
        { session_question_id: sessionQuestionId, selected_option_index: selectedOptionIndex },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Store one prediction of the partner's answer, scored against the original. */
export const submitCouplePrediction = async (
    sessionQuestionId: string,
    predictedOptionIndex: number
): Promise<CoupleCurrentState> => {
    const response = await axios.post(
        `${API_URL}/games/couple-questionnaire/predictions`,
        { session_question_id: sessionQuestionId, predicted_option_index: predictedOptionIndex },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

// --- Plot Twist: asynchronous cooperative storytelling for couples ---
//
// The story executes a published, versioned package deterministically --
// generation happens on the admin side, never during gameplay.

export type PlotTwistStage =
  | 'no_game'
  | 'selecting_partner'
  | 'awaiting_you'
  | 'awaiting_partner'
  | 'completed';

export interface PlotTwistChoiceOption {
  choice_id: string;
  text: string;
  consequence_text?: string | null;
  reward_tier?: string | null;
}

export interface PlotTwistSceneState {
  scene_id: string;
  scene_type: string;
  title: string;
  narrative_text: string;
  actor: string;
  choices: PlotTwistChoiceOption[];
  is_terminal: boolean;
  visited_count: number;
}

export interface PlotTwistDecisionEntry {
  seq: number;
  scene_id: string;
  scene_title?: string | null;
  actor_user_id: string;
  actor_name?: string | null;
  choice_id: string;
  choice_text?: string | null;
  consequence_text?: string | null;
  created_at: string;
}

export interface PlotTwistResultState {
  ending_id?: string | null;
  ending_type?: string | null;
  ending_title?: string | null;
  narrative?: string | null;
  success: boolean;
  treasures: string[];
  decisions_made: number;
  completed_at?: string | null;
}

export interface PlotTwistState {
  stage: PlotTwistStage;
  game_id: string | null;
  session_id: string | null;
  my_selection: { selected_user_id: string; selected_user_name: string } | null;
  partner: { id: string; name: string } | null;
  scene: PlotTwistSceneState | null;
  state_version: number;
  decisions_made: number;
  history: PlotTwistDecisionEntry[];
  result: PlotTwistResultState | null;
}

/** The caller's place in the active Plot Twist event. Never creates gameplay rows. */
export const getPlotTwistState = async (): Promise<PlotTwistState> => {
  const response = await axios.get(`${API_URL}/games/plot-twist`, {
    headers: getAuthHeaders(),
  });
  return response.data;
};

/** One page of participants the caller may choose, excluding only themselves. */
export const getPlotTwistPartners = (
  params?: CouplePartnersQuery
): Promise<CouplePartnersPage> =>
  fetchCouplePartners('/games/plot-twist/partners', params);

/** Record the caller's partner choice through the shared selection. */
export const selectPlotTwistPartner = async (
  selectedUserId: string
): Promise<PlotTwistState> => {
  const response = await axios.post(
    `${API_URL}/games/plot-twist/selection`,
    { selected_user_id: selectedUserId },
    { headers: getAuthHeaders() }
  );
  return response.data;
};

/** Enter the active event: create (or fetch) this couple's story session. */
export const startPlotTwistEvent = async (): Promise<PlotTwistState> => {
  const response = await axios.post(
    `${API_URL}/games/plot-twist/start`,
    {},
    { headers: getAuthHeaders() }
  );
  return response.data;
};

/** Submit the assigned decision. Retries of a recorded turn answer the stored outcome. */
export const submitPlotTwistChoice = async (
  choiceId: string,
  stateVersion: number
): Promise<PlotTwistState> => {
  const response = await axios.post(
    `${API_URL}/games/plot-twist/choices`,
    { choice_id: choiceId, state_version: stateVersion },
    { headers: getAuthHeaders() }
  );
  return response.data;
};

export interface PlotTwistBlueprint {
  id: string;
  title: string;
  difficulty: string;
  config: Record<string, unknown>;
  blueprint: Record<string, unknown>;
  status: string;
  validation_errors: Array<{ scene_id?: string | null; code: string; message: string }>;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface PlotTwistPackage {
  id: string;
  blueprint_id: string;
  version: number;
  title: string;
  package: Record<string, unknown>;
  status: string;
  validation_errors: Array<{ scene_id?: string | null; code: string; message: string }>;
  published_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface PlotTwistSimulation {
  steps: Array<Record<string, unknown>>;
  ending_id: string | null;
  ending_title: string | null;
  final_state: Record<string, unknown>;
  treasures: string[];
  valid: boolean;
  error: string | null;
}

/** Store the generation parameters (difficulty plus optional overrides) as a draft. */
export const createPlotTwistBlueprint = async (
  payload: Record<string, unknown>
): Promise<PlotTwistBlueprint> => {
  const response = await axios.post(`${API_URL}/admin/plot-twist/blueprints`, payload, {
    headers: getAuthHeaders(),
  });
  return response.data;
};

export const getPlotTwistBlueprints = async (): Promise<PlotTwistBlueprint[]> => {
  const response = await axios.get(`${API_URL}/admin/plot-twist/blueprints`, {
    headers: getAuthHeaders(),
  });
  return response.data;
};

export const getPlotTwistBlueprint = async (blueprintId: string): Promise<PlotTwistBlueprint> => {
  const response = await axios.get(`${API_URL}/admin/plot-twist/blueprints/${blueprintId}`, {
    headers: getAuthHeaders(),
  });
  return response.data;
};

/** Run Stage A blueprint generation (bounded repair included). */
export const generatePlotTwistBlueprint = async (
  blueprintId: string
): Promise<PlotTwistBlueprint> => {
  const response = await axios.post(
    `${API_URL}/admin/plot-twist/blueprints/${blueprintId}/generate`,
    {},
    { headers: getAuthHeaders() }
  );
  return response.data;
};

export const approvePlotTwistBlueprint = async (
  blueprintId: string
): Promise<PlotTwistBlueprint> => {
  const response = await axios.post(
    `${API_URL}/admin/plot-twist/blueprints/${blueprintId}/approve`,
    {},
    { headers: getAuthHeaders() }
  );
  return response.data;
};

/** Run Stage B story-package generation as a new version. */
export const generatePlotTwistPackage = async (
  blueprintId: string
): Promise<PlotTwistPackage> => {
  const response = await axios.post(
    `${API_URL}/admin/plot-twist/blueprints/${blueprintId}/packages`,
    {},
    { headers: getAuthHeaders() }
  );
  return response.data;
};

export const getPlotTwistPackages = async (
  blueprintId?: string
): Promise<PlotTwistPackage[]> => {
  const response = await axios.get(`${API_URL}/admin/plot-twist/packages`, {
    headers: getAuthHeaders(),
    params: blueprintId ? { blueprint_id: blueprintId } : {},
  });
  return response.data;
};

export const getPlotTwistPackage = async (packageId: string): Promise<PlotTwistPackage> => {
  const response = await axios.get(`${API_URL}/admin/plot-twist/packages/${packageId}`, {
    headers: getAuthHeaders(),
  });
  return response.data;
};

/** Regenerate one scene in place; a failed repair keeps the prior draft. */
export const regeneratePlotTwistScene = async (
  packageId: string,
  sceneId: string
): Promise<PlotTwistPackage> => {
  const response = await axios.post(
    `${API_URL}/admin/plot-twist/packages/${packageId}/scenes/${sceneId}/regenerate`,
    {},
    { headers: getAuthHeaders() }
  );
  return response.data;
};

/** Manually edit supported story fields; re-validates, published is immutable. */
export const updatePlotTwistPackage = async (
  packageId: string,
  storyPackage: Record<string, unknown>
): Promise<PlotTwistPackage> => {
  const response = await axios.put(
    `${API_URL}/admin/plot-twist/packages/${packageId}`,
    { package: storyPackage },
    { headers: getAuthHeaders() }
  );
  return response.data;
};

/** Walk choice ids from the opening and inspect the outcome, without playing. */
export const simulatePlotTwistPackage = async (
  packageId: string,
  choiceIds: string[]
): Promise<PlotTwistSimulation> => {
  const response = await axios.post(
    `${API_URL}/admin/plot-twist/packages/${packageId}/simulate`,
    { choice_ids: choiceIds },
    { headers: getAuthHeaders() }
  );
  return response.data;
};

/** Publish a validated package version; sessions pin this version. */
export const publishPlotTwistPackage = async (
  packageId: string
): Promise<PlotTwistPackage> => {
  const response = await axios.post(
    `${API_URL}/admin/plot-twist/packages/${packageId}/publish`,
    {},
    { headers: getAuthHeaders() }
  );
  return response.data;
};

// --- Automated AI session generation (Plot Twist / Case Closed) ---
//
// Both games generate their sessions automatically on their configured
// cadence: the scheduler enqueues one cycle at the boundary and a background
// worker runs the blueprint -> approve -> package -> validate -> publish
// pipeline. The console therefore only reads the published session, pages its
// history and can revoke; the blueprint/package calls above remain the
// pipeline's own recovery surface and are no longer part of the admin flow.

/** The lifecycle state of one automated generation cycle. */
export type AiGenerationStatus =
  | 'queued'
  | 'running'
  | 'retry_wait'
  | 'published'
  | 'failed'
  | 'cancelled';

/** The latest generation cycle for an AI game, as the console reports it. */
export interface AiGenerationState {
  id: string;
  game_id: string;
  cycle_start_date: string | null;
  cycle_end_date: string | null;
  difficulty: string;
  status: AiGenerationStatus;
  attempt: number;
  max_attempts: number;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  next_attempt_at: string | null;
  blueprint_id: string | null;
  package_id: string | null;
  error_code: string | null;
  last_error: string | null;
  created_by: string;
}

/** One session row in an AI game's backend-paginated history, newest first. */
export interface AiSessionHistoryEntry {
  id: string;
  status: string;
  period_start_date: string | null;
  period_end_date: string | null;
  package_id: string | null;
  title: string | null;
  /** True when an administrator revoked this session rather than it expiring. */
  revoked: boolean;
  created_by: string;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
  /** Case Closed only: whether the culprit was revealed and everyone scored. */
  finalized?: boolean;
}

export interface AiSessionHistoryPage {
  sessions: AiSessionHistoryEntry[];
  total: number;
  skip: number;
  limit: number;
}

/** One story choice inside a published Plot Twist scene. */
export interface PlotTwistSessionChoice {
  id: string;
  text: string;
  consequence_text?: string | null;
  next_scene_id?: string | null;
  reward_tier?: string | null;
}

/** One scene of the published Plot Twist story, as the console renders it. */
export interface PlotTwistSessionScene {
  id: string;
  type: string;
  title: string;
  narrative_text: string;
  actor: string;
  slot?: string;
  choices: PlotTwistSessionChoice[];
  is_terminal: boolean;
  ending_id?: string | null;
}

export interface PlotTwistSessionEnding {
  id: string;
  type: string;
  title: string;
  narrative: string;
  success?: boolean;
  reward_tier?: string | null;
}

export interface PlotTwistSessionPackage {
  title?: string;
  summary?: string;
  opening_scene_id?: string;
  scenes?: PlotTwistSessionScene[];
  endings?: PlotTwistSessionEnding[];
}

export interface PlotTwistSessionOverview {
  game_id: string;
  session: {
    id: string;
    status: string;
    period_start_date: string | null;
    period_end_date: string | null;
    package_id: string | null;
    title: string | null;
    package: PlotTwistSessionPackage;
    created_by: string;
    started_at: string | null;
    ended_at: string | null;
    created_at: string;
  } | null;
  generation: AiGenerationState | null;
}

/** One suspect of the published Case Closed scenario. */
export interface CaseClosedSessionSuspect {
  id: string;
  name: string;
  background: string;
  relationship: string;
}

/** One investigable lead of the published Case Closed scenario. */
export interface CaseClosedSessionLead {
  id: string;
  title: string;
  description: string;
  discovery_text: string;
  next_lead_ids: string[];
  suspect_ids?: string[];
}

export interface CaseClosedSessionPackage {
  title?: string;
  opening_narrative?: string;
  suspects?: CaseClosedSessionSuspect[];
  evidence?: Array<{ id: string; title: string; description: string; kind?: string }>;
  lead_nodes?: CaseClosedSessionLead[];
  solution?: { culprit_id?: string; motive?: string };
  solution_reveal?: {
    title?: string;
    narrative?: string;
    why_others_ruled_out?: string;
  };
  time_scoring_config?: { half_life_seconds?: number; max_points?: number };
}

export interface CaseClosedSessionOverview {
  game_id: string;
  session: {
    id: string;
    status: string;
    period_start_date: string | null;
    period_end_date: string | null;
    package_id: string | null;
    title: string | null;
    package: CaseClosedSessionPackage;
    created_by: string;
    started_at: string | null;
    ended_at: string | null;
    created_at: string;
    finalized: boolean;
    finalized_at: string | null;
    scoring_half_life_seconds?: number;
    scoring_max_points?: number;
  } | null;
  generation: AiGenerationState | null;
}

export interface AiSessionRevocation {
  game_id: string;
  action: 'revoked' | 'none';
  session_id: string | null;
  revoked_until: string | null;
}

/** The current Plot Twist session's published content plus generation state. */
export const getPlotTwistSessionOverview = async (): Promise<PlotTwistSessionOverview> => {
  const response = await axios.get(`${API_URL}/admin/plot-twist/session`, {
    headers: getAuthHeaders(),
  });
  return response.data;
};

/** One backend page of Plot Twist session history, newest first. */
export const getPlotTwistSessionHistory = async (params?: {
  skip?: number;
  limit?: number;
}): Promise<AiSessionHistoryPage> => {
  const response = await axios.get(`${API_URL}/admin/plot-twist/sessions`, {
    headers: getAuthHeaders(),
    params: {
      ...(params?.skip !== undefined ? { skip: params.skip } : {}),
      ...(params?.limit !== undefined ? { limit: params.limit } : {}),
    },
  });
  return response.data;
};

/** End the live Plot Twist session (or cancel its generation) without replacing it. */
export const revokePlotTwistSession = async (): Promise<AiSessionRevocation> => {
  const response = await axios.post(
    `${API_URL}/admin/plot-twist/session/revoke`,
    {},
    { headers: getAuthHeaders() }
  );
  return response.data;
};

// --- Case Closed (cooperative mystery): player + admin ---

export type CaseClosedStage =
  | 'no_game'
  | 'selecting_partner'
  | 'investigating'
  | 'accusation_locked'
  | 'completed';

export interface CaseClosedLead {
  id: string;
  title: string;
  description: string;
  investigated: boolean;
  available: boolean;
  depth: number;
}

export interface CaseClosedEvidence {
  id: string;
  title: string;
  description: string;
  discovered_at: string | null;
  discovered_by: string | null;
  source_lead_id: string | null;
  suspect_ids: string[];
  kind: string;
}

export interface CaseClosedSuspect {
  id: string;
  name: string;
  background: string;
  relationship: string;
}

export interface CaseClosedState {
  stage: CaseClosedStage;
  game_id: string | null;
  session_id: string | null;
  case_title: string | null;
  opening_narrative: string | null;
  suspects: CaseClosedSuspect[];
  available_leads: CaseClosedLead[];
  my_path: string[];
  investigated_lead_ids: string[];
  evidence_board: CaseClosedEvidence[];
  my_remaining: number;
  partner_remaining: number;
  accusation: {
    my_proposal: string | null;
    partner_proposal: string | null;
    my_confirmed: string | null;
    partner_confirmed: string | null;
    locked_suspect_id: string | null;
    locked_at: string | null;
  };
  timer: {
    started: boolean;
    first_investigation_at: string | null;
    locked_at: string | null;
    elapsed_seconds: number | null;
  };
  session_status: string | null;
  event_deadline: string | null;
  state_version: number;
  finalized: boolean;
}

export interface CaseClosedInvestigateResult {
  state: CaseClosedState;
  lead_id: string;
  discovery_text: string;
  already_known: boolean;
  replay: boolean;
}

export interface CaseClosedResult {
  locked_suspect_id: string | null;
  correct: boolean | null;
  score: number | null;
  elapsed_seconds: number | null;
  finalized: boolean;
  culprit_id: string | null;
  solution_title: string | null;
  solution_narrative: string | null;
  why_others_ruled_out: string | null;
}

export const getCaseClosedState = async (): Promise<CaseClosedState> => {
  const response = await axios.get(`${API_URL}/games/case-closed`, { headers: getAuthHeaders() });
  return response.data;
};

export const getCaseClosedPartners = (
  params?: CouplePartnersQuery
): Promise<CouplePartnersPage> =>
  fetchCouplePartners('/games/case-closed/partners', params);

export const selectCaseClosedPartner = async (selectedUserId: string): Promise<CaseClosedState> => {
  const response = await axios.post(
    `${API_URL}/games/case-closed/selection`,
    { selected_user_id: selectedUserId },
    { headers: getAuthHeaders() }
  );
  return response.data;
};

export const startCaseClosedEvent = async (): Promise<CaseClosedState> => {
  const response = await axios.post(`${API_URL}/games/case-closed/start`, {}, { headers: getAuthHeaders() });
  return response.data;
};

export const investigateCaseClosedLead = async (
  leadId: string,
  stateVersion: number
): Promise<CaseClosedInvestigateResult> => {
  const response = await axios.post(
    `${API_URL}/games/case-closed/investigate`,
    { lead_id: leadId, state_version: stateVersion },
    { headers: getAuthHeaders() }
  );
  return response.data;
};

export const proposeCaseClosedAccusation = async (suspectId: string): Promise<CaseClosedState> => {
  const response = await axios.post(
    `${API_URL}/games/case-closed/accusation/propose`,
    { suspect_id: suspectId },
    { headers: getAuthHeaders() }
  );
  return response.data;
};

export const confirmCaseClosedAccusation = async (suspectId: string): Promise<CaseClosedState> => {
  const response = await axios.post(
    `${API_URL}/games/case-closed/accusation/confirm`,
    { suspect_id: suspectId },
    { headers: getAuthHeaders() }
  );
  return response.data;
};

export const getCaseClosedResult = async (): Promise<CaseClosedResult> => {
  const response = await axios.get(`${API_URL}/games/case-closed/result`, { headers: getAuthHeaders() });
  return response.data;
};

export interface CaseClosedBlueprint {
  id: string;
  title: string;
  difficulty: string;
  config: Record<string, unknown>;
  blueprint: Record<string, unknown>;
  status: string;
  validation_errors: Array<{ lead_id?: string | null; code: string; message: string }>;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface CaseClosedPackage {
  id: string;
  blueprint_id: string;
  version: number;
  title: string;
  package: Record<string, unknown>;
  status: string;
  validation_errors: Array<{ lead_id?: string | null; code: string; message: string }>;
  published_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export const createCaseClosedBlueprint = async (payload: Record<string, unknown>): Promise<CaseClosedBlueprint> => {
  const response = await axios.post(`${API_URL}/admin/case-closed/blueprints`, payload, { headers: getAuthHeaders() });
  return response.data;
};

export const getCaseClosedBlueprints = async (): Promise<CaseClosedBlueprint[]> => {
  const response = await axios.get(`${API_URL}/admin/case-closed/blueprints`, { headers: getAuthHeaders() });
  return response.data;
};

export const generateCaseClosedBlueprint = async (blueprintId: string): Promise<CaseClosedBlueprint> => {
  const response = await axios.post(`${API_URL}/admin/case-closed/blueprints/${blueprintId}/generate`, {}, { headers: getAuthHeaders() });
  return response.data;
};

export const approveCaseClosedBlueprint = async (blueprintId: string): Promise<CaseClosedBlueprint> => {
  const response = await axios.post(`${API_URL}/admin/case-closed/blueprints/${blueprintId}/approve`, {}, { headers: getAuthHeaders() });
  return response.data;
};

export const generateCaseClosedPackage = async (blueprintId: string): Promise<CaseClosedPackage> => {
  const response = await axios.post(`${API_URL}/admin/case-closed/blueprints/${blueprintId}/packages`, {}, { headers: getAuthHeaders() });
  return response.data;
};

export const getCaseClosedPackages = async (blueprintId?: string): Promise<CaseClosedPackage[]> => {
  const response = await axios.get(`${API_URL}/admin/case-closed/packages`, {
    headers: getAuthHeaders(),
    params: blueprintId ? { blueprint_id: blueprintId } : {},
  });
  return response.data;
};

export const publishCaseClosedPackage = async (packageId: string): Promise<CaseClosedPackage> => {
  const response = await axios.post(`${API_URL}/admin/case-closed/packages/${packageId}/publish`, {}, { headers: getAuthHeaders() });
  return response.data;
};

/** The current Case Closed session's published content plus generation state. */
export const getCaseClosedSessionOverview = async (): Promise<CaseClosedSessionOverview> => {
  const response = await axios.get(`${API_URL}/admin/case-closed/session`, {
    headers: getAuthHeaders(),
  });
  return response.data;
};

/** One backend page of Case Closed session history, newest first. */
export const getCaseClosedSessionHistory = async (params?: {
  skip?: number;
  limit?: number;
}): Promise<AiSessionHistoryPage> => {
  const response = await axios.get(`${API_URL}/admin/case-closed/sessions`, {
    headers: getAuthHeaders(),
    params: {
      ...(params?.skip !== undefined ? { skip: params.skip } : {}),
      ...(params?.limit !== undefined ? { limit: params.limit } : {}),
    },
  });
  return response.data;
};

/** End the live Case Closed event (or cancel its generation) without replacing it. */
export const revokeCaseClosedSession = async (): Promise<AiSessionRevocation> => {
  const response = await axios.post(
    `${API_URL}/admin/case-closed/session/revoke`,
    {},
    { headers: getAuthHeaders() }
  );
  return response.data;
};

// --- Case Closed detective thread (per-couple chat inside the game) ---

export interface CaseClosedThread {
  game_id: string;
  session_id: string;
  partner: { id: string; name: string };
  conversation_limit: number;
  participant_limit: number;
  /** False once either detective confirmed the accusation or the case ended. */
  chat_open: boolean;
  closed_reason: string | null;
}

export interface CaseClosedChatMessage {
  id: string;
  game_id: string | null;
  session_id: string;
  sender_user_id: string;
  body: string;
  created_at: string;
  is_read: boolean;
  is_sender?: boolean;
}

export interface CaseClosedMessageQuota {
  conversation_limit: number;
  participant_limit: number;
  conversation_count: number;
  participant_count: number;
  conversation_remaining: number;
  participant_remaining: number;
  can_send: boolean;
  limit_reached: 'participant' | 'conversation' | null;
}

export interface CaseClosedTranscript {
  game_id: string;
  session_id: string;
  messages: CaseClosedChatMessage[];
  count: number;
  total: number;
  quota: CaseClosedMessageQuota;
  /**
   * Whether the thread still accepts turns. False the moment either detective
   * confirmed the accusation (or the case ended): the dialog closes its
   * composer and drops the live socket; the transcript stays readable.
   */
  chat_open: boolean;
  /** Why the thread takes no new turns, when it is closed. */
  closed_reason: string | null;
}

export interface CaseClosedSentMessage extends CaseClosedChatMessage {
  quota: CaseClosedMessageQuota;
}

export const getCaseClosedThread = async (): Promise<CaseClosedThread> => {
  const response = await axios.get(`${API_URL}/games/case-closed/messages/thread`, { headers: getAuthHeaders() });
  return response.data;
};

export const getCaseClosedMessages = async (limit?: number, before?: string): Promise<CaseClosedTranscript> => {
  const response = await axios.get(`${API_URL}/games/case-closed/messages`, {
    headers: getAuthHeaders(),
    params: { ...(limit ? { limit } : {}), ...(before ? { before } : {}) },
  });
  return response.data;
};

export const sendCaseClosedMessage = async (body: string): Promise<CaseClosedSentMessage> => {
  const response = await axios.post(`${API_URL}/games/case-closed/messages`, { body }, { headers: getAuthHeaders() });
  return response.data;
};

/**
 * Report a partner's detective-thread turn. Mirrors the matchmaking chat report:
 * only the recipient may report a turn, one report per turn. The complaint lands
 * in the admin "Chat messages" queue with `kind: "case_closed"`.
 *
 * The server's `detail` is re-thrown as the Error message so the dialog can tell
 * a duplicate ("You have already reported this message") from any other failure.
 */
export const reportCaseClosedMessage = async (messageId: string, reason: string): Promise<{ id: string }> => {
  try {
    const response = await axios.post(
      `${API_URL}/games/case-closed/messages/report`,
      { message_id: messageId, reason },
      { headers: getAuthHeaders() }
    );
    return response.data;
  } catch (error: unknown) {
    const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    throw new Error(
      typeof detail === 'string' && detail ? detail : 'Failed to report message',
      { cause: error }
    );
  }
};

// --- Game configuration, featured spotlight, scheduling and selection ---
//
// The separated model: per-game availability/cadence, the one-spotlight-per-day
// schedule, horizon-bounded future words, and the server-derived selection list
// (hidden games never appear; disabled/pending games arrive marked unplayable).

/** One game's availability, content mode, reset cadence and grid size, as stored. */
export interface GameConfig {
    game_id: string;
    enabled: boolean;
    is_hidden: boolean;
    content_mode: 'random' | 'manual' | null;
    reset_cadence: 'daily' | 'every_2_days' | 'every_3_days' | 'every_5_days' | 'weekly';
    /** The Two-Key Escape maze edge length. Null for every other game. */
    grid_size: number | null;
    /** The difficulty the automated pipeline uses for the next Plot Twist / Case Closed cycle. */
    difficulty: 'easy' | 'medium' | 'hard' | null;
}

/** Every game's configuration. */
export const getGameConfigs = async (): Promise<GameConfig[]> => {
    const response = await axios.get(`${API_URL}/admin/games/config`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Patch one game's availability, content mode, reset cadence, grid size or difficulty. */
export const updateGameConfig = async (
    gameId: string,
    payload: Partial<
        Pick<
            GameConfig,
            'enabled' | 'is_hidden' | 'content_mode' | 'reset_cadence' | 'grid_size' | 'difficulty'
        >
    >
): Promise<GameConfig> => {
    const response = await axios.put(`${API_URL}/admin/games/config/${gameId}`, payload, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** One game's finished-round counts over the console's three windows. */
export interface GamePlayTotals {
    game_id: string;
    plays_today: number;
    plays_this_week: number;
    plays_all_time: number;
}

/** Live play telemetry across every game, for the games console overview. */
export interface GameStatsSummary {
    server_date: string;
    plays_today: number;
    plays_this_week: number;
    plays_all_time: number;
    players_today: number;
    players_this_week: number;
    games: GamePlayTotals[];
}

/**
 * The console's play figures, read from the games' native participation
 * tables rather than the league ledger, so a round played today appears
 * immediately instead of waiting for its event to finalize. The same
 * collectors answer `GET /games/summary` for a player, so both dashboards
 * agree on what counts as a finished round.
 */
export const getAdminGameStats = async (): Promise<GameStatsSummary> => {
    const response = await axios.get(`${API_URL}/admin/games/stats`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** One featured date: which game holds the spotlight on which UTC day. */
export interface FeaturedGame {
    id: string;
    game_date: string;
    game_id: string;
    created_by: string;
    created_at: string;
    updated_at: string;
}

/** Every featured date, newest day first. */
export const getFeaturedGames = async (): Promise<FeaturedGame[]> => {
    const response = await axios.get(`${API_URL}/admin/featured`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Pin one game as featured for one UTC date. A date that already has one answers 409. */
export const setFeaturedGame = async (
    gameDate: string,
    gameId: string
): Promise<FeaturedGame> => {
    const response = await axios.post(
        `${API_URL}/admin/featured`,
        { game_date: gameDate, game_id: gameId },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/**
 * Set one game as featured for one UTC date, replacing any current pick.
 *
 * The atomic path for the per-game "Set as Featured" action: when another
 * game holds the date it is removed in the same transaction. Replacing never
 * rewrites league history -- awards snapshot `is_featured` at finalization
 * time, so points already earned keep their 2x while only future plays earn
 * 2x on the new game.
 */
export const replaceFeaturedGame = async (
    gameDate: string,
    gameId: string
): Promise<FeaturedGame> => {
    const response = await axios.put(
        `${API_URL}/admin/featured`,
        { game_date: gameDate, game_id: gameId },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Remove the featured pick for one UTC date. */
export const clearFeaturedGame = async (gameDate: string): Promise<void> => {
    await axios.delete(`${API_URL}/admin/featured/${gameDate}`, {
        headers: getAuthHeaders(),
    });
};

/**
 * Feature one game for a whole span, start to end inclusive.
 *
 * Every date in the span must be free of other games (the first taken date
 * answers 409), and the span merged with the same game's bordering picks may
 * not run more than a week (422). Returns the created rows.
 */
export const setFeaturedRange = async (
    startDate: string,
    endDate: string,
    gameId: string
): Promise<FeaturedGame[]> => {
    const response = await axios.post(
        `${API_URL}/admin/featured/range`,
        { game_id: gameId, start_date: startDate, end_date: endDate },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/**
 * Feature one game for a whole span, erasing any other picks it covers.
 *
 * The override path for the schedule page: the admin confirms once against
 * the listed dates, and the server replaces them in one transaction. An
 * overlong consecutive run is still a 422, and league history is untouched.
 */
export const replaceFeaturedRange = async (
    startDate: string,
    endDate: string,
    gameId: string
): Promise<FeaturedGame[]> => {
    const response = await axios.put(
        `${API_URL}/admin/featured/range`,
        { game_id: gameId, start_date: startDate, end_date: endDate },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Evaluate every game's session boundary now (the manual 00:01 UTC trigger). */
export const runScheduledResets = async (): Promise<{
    resets: Record<string, { action: string; session_id?: string | null }>;
    active: Record<string, number>;
}> => {
    const response = await axios.post(
        `${API_URL}/admin/games/run-resets`,
        {},
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** A manual word waiting for its session period to begin. */
export interface ScheduledWord {
    id: string;
    game_id: string;
    target_date: string;
    word: string;
    created_by: string;
}

/** Schedule a manual Wordle word for a future period (bounded horizon). */
export const scheduleWordleWord = async (
    targetDate: string,
    word: string
): Promise<ScheduledWord> => {
    const response = await axios.post(
        `${API_URL}/admin/wordle/schedule`,
        { target_date: targetDate, word },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Every scheduled Wordle word, earliest date first. */
export const getScheduledWordleWords = async (): Promise<ScheduledWord[]> => {
    const response = await axios.get(`${API_URL}/admin/wordle/scheduled`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Withdraw the scheduled Wordle word for one date. */
export const deleteScheduledWordleWord = async (targetDate: string): Promise<void> => {
    await axios.delete(`${API_URL}/admin/wordle/scheduled/${targetDate}`, {
        headers: getAuthHeaders(),
    });
};

/** Schedule a manual Anagram word for a future period (bounded horizon). */
export const scheduleAnagramWord = async (
    targetDate: string,
    word: string
): Promise<ScheduledWord> => {
    const response = await axios.post(
        `${API_URL}/admin/anagram/schedule`,
        { target_date: targetDate, word },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Every scheduled Anagram word, earliest date first. */
export const getScheduledAnagramWords = async (): Promise<ScheduledWord[]> => {
    const response = await axios.get(`${API_URL}/admin/anagram/scheduled`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Withdraw the scheduled Anagram word for one date. */
export const deleteScheduledAnagramWord = async (targetDate: string): Promise<void> => {
    await axios.delete(`${API_URL}/admin/anagram/scheduled/${targetDate}`, {
        headers: getAuthHeaders(),
    });
};

/** The current Wordle session for the admin screen, or 404 when there is none. */
export const getCurrentAdminWordleSession = async (): Promise<WordleGame> => {
    const response = await axios.get(`${API_URL}/admin/wordle/current`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** The current Anagram session for the admin screen, or 404 when there is none. */
export const getCurrentAdminAnagramSession = async (): Promise<AnagramGame> => {
    const response = await axios.get(`${API_URL}/admin/anagram/current`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** The current global Couple Questionnaire session, or 404 when there is none. */
export const getCurrentAdminCoupleSession = async (): Promise<{
    id: string;
    game_date: string;
    status: string;
    period_start_date: string | null;
    period_end_date: string | null;
}> => {
    const response = await axios.get(`${API_URL}/admin/couple/session`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** One game as the selection screen sees it. Hidden games never appear. */
export interface GameSelectEntry {
    game_id: string;
    name: string;
    enabled: boolean;
    playable: boolean;
    session_status: string | null;
    featured: boolean;
    message: string | null;
}

/** The server-derived selection list plus today's featured pick. */
export interface GameSelectResponse {
    games: GameSelectEntry[];
    featured_game_id: string | null;
    server_date: string;
}

/** Every discoverable game with its playability. */
export const getGameSelection = async (): Promise<GameSelectResponse> => {
    const response = await axios.get(`${API_URL}/games/select`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Today's featured game (UTC), if the day has a pick. */
export const getFeaturedToday = async (): Promise<{
    game_id: string | null;
    game_date: string | null;
    server_date: string;
}> => {
    const response = await axios.get(`${API_URL}/games/featured`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** One finished round in the caller's own history, newest first. */
export interface PlayerRecentPlay {
    game_id: string;
    played_at: string;
    summary: string;
    score: number | null;
    score_label: string | null;
}

/** The caller's finished rounds, streaks and this week's activity. */
export interface PlayerGameSummary {
    games_played: number;
    current_streak_days: number;
    longest_streak_days: number;
    plays_this_week: number;
    games_this_week: string[];
    recent_plays: PlayerRecentPlay[];
}

/**
 * The caller's own play history across every game, for the dashboard.
 *
 * Composed server-side from the games' native participation tables, so a
 * round played today is in this list immediately -- unlike league awards,
 * which are issued only when an event finalizes.
 */
export const getPlayerGameSummary = async (): Promise<PlayerGameSummary> => {
    const response = await axios.get(`${API_URL}/games/summary`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

// --- Reaction Time Configuration Endpoints ---

/**
 * One featured Reaction Time game, as `GET /admin/reaction-time` returns it.
 *
 * There is deliberately no mode, word, answer or timing field here: featuring
 * a day stores the day and who featured it, and nothing else. The mechanics
 * are fixed by the application.
 */
export interface ReactionTimeGame {
    id: string;
    game_date: string;
    created_by: string;
    created_at: string;
    updated_at: string;
}

/**
 * The fields `POST /admin/reaction-time` accepts.
 *
 * Only the day. It identifies the featured instance and defaults to today in
 * UTC when omitted.
 */
export interface ReactionTimeGamePayload {
    game_date?: string;
}

/** Feature the Reaction Time game for one day. A day that is already featured answers 409. */
export const createReactionTimeGame = async (
    payload: ReactionTimeGamePayload
): Promise<ReactionTimeGame> => {
    const response = await axios.post(`${API_URL}/admin/reaction-time`, payload, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Every featured Reaction Time date, newest day first. */
export const getAdminReactionTimeGames = async (): Promise<ReactionTimeGame[]> => {
    const response = await axios.get(`${API_URL}/admin/reaction-time`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** One featured Reaction Time date by id. A malformed id answers 400, an unknown one 404. */
export const getAdminReactionTimeGame = async (
    gameId: string
): Promise<ReactionTimeGame> => {
    const response = await axios.get(`${API_URL}/admin/reaction-time/${gameId}`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** The current Reaction Time session for the admin screen, or 404 when there is none. */
export const getCurrentAdminReactionTimeSession = async (): Promise<{
    id: string;
    game_date: string;
    status: string;
    period_start_date: string | null;
    period_end_date: string | null;
}> => {
    const response = await axios.get(`${API_URL}/admin/reaction-time/session`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

// --- Connections Gameplay Endpoints ---

/**
 * Today's Connections puzzle and this player's submission state.
 *
 * `words` is this player's stable shuffled tile order -- empty when nothing
 * is playable or once they have submitted. `submitted` means the one
 * submission is consumed and the screen shows a confirmation, never a board.
 * The canonical groups never travel on this read.
 */
export interface ConnectionsCurrentGame {
    game_id: string | null;
    game_date: string | null;
    words: string[];
    submitted: boolean;
    /** Mirrors the session lifecycle; `pending` means the puzzle is not set yet. */
    session_status?: string | null;
    /** Sentence the screen shows when the session is pending. */
    message?: string | null;
}

/** The confirmation a submitted player reads -- and all they read. */
export interface ConnectionsSubmitResponse {
    game_id: string;
    submitted: boolean;
    submitted_at: string;
    message: string;
}

/** Today's playable puzzle and this player's submission state. Never creates a row. */
export const getCurrentConnections = async (): Promise<ConnectionsCurrentGame> => {
    const response = await axios.get(`${API_URL}/games/connections`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/**
 * Score one final arrangement against one puzzle and store it for the current
 * player. Exactly one submission per player per session; a second submit --
 * concurrent or direct -- is refused.
 */
export const submitConnections = async (
    gameId: string,
    groups: string[][]
): Promise<ConnectionsSubmitResponse> => {
    const response = await axios.post(
        `${API_URL}/games/connections/submit`,
        { game_id: gameId, groups },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

// --- Connections Configuration Endpoints ---

/**
 * One Connections session as the admin screen reads it: the day, its sixteen
 * words in four canonical groups, and its lifecycle state. `groups` is empty
 * while the session is pending.
 */
export interface ConnectionsGame {
    id: string;
    game_date: string;
    groups: string[][];
    status?: string;
    period_start_date?: string | null;
    period_end_date?: string | null;
    created_by: string;
    created_at: string;
    updated_at: string;
}

/** The fields `POST /admin/connections` accepts: sixteen words in four groups of four. */
export interface ConnectionsPuzzlePayload {
    groups: string[][];
}

/** A manually configured Connections puzzle waiting for its session period. */
export interface ConnectionsScheduledPuzzle {
    id: string;
    target_date: string;
    groups: string[][];
    created_by: string;
}

/** Publish the current session: end it and open a brand-new one with this puzzle. */
export const createConnectionsGame = async (
    payload: ConnectionsPuzzlePayload
): Promise<ConnectionsGame> => {
    const response = await axios.post(`${API_URL}/admin/connections`, payload, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** One page of every Connections session, newest first, with the total beside it. */
export const getAdminConnectionsGames = async (params?: {
    skip?: number;
    limit?: number;
}): Promise<AdminGamesPage<ConnectionsGame>> => {
    const response = await axios.get(`${API_URL}/admin/connections`, {
        headers: getAuthHeaders(),
        params: {
            ...(params?.skip !== undefined ? { skip: params.skip } : {}),
            ...(params?.limit !== undefined ? { limit: params.limit } : {}),
        },
    });
    const { rows, total } = normalizeGamesPage<ConnectionsGame>(response.data, 'games');
    return { games: rows, total, skip: params?.skip ?? 0, limit: params?.limit ?? rows.length };
};

/** The current Connections session for the admin screen, or 404 when there is none. */
export const getCurrentAdminConnectionsSession = async (): Promise<ConnectionsGame> => {
    const response = await axios.get(`${API_URL}/admin/connections/current`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Schedule a manual Connections puzzle for a future period (bounded horizon). */
export const scheduleConnectionsPuzzle = async (
    targetDate: string,
    groups: string[][]
): Promise<ConnectionsScheduledPuzzle> => {
    const response = await axios.post(
        `${API_URL}/admin/connections/schedule`,
        { target_date: targetDate, groups },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Every scheduled Connections puzzle, earliest date first. */
export const getScheduledConnectionsPuzzles = async (): Promise<ConnectionsScheduledPuzzle[]> => {
    const response = await axios.get(`${API_URL}/admin/connections/scheduled`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Withdraw the scheduled Connections puzzle for one date. */
export const deleteScheduledConnectionsPuzzle = async (targetDate: string): Promise<void> => {
    await axios.delete(`${API_URL}/admin/connections/scheduled/${targetDate}`, {
        headers: getAuthHeaders(),
    });
};

// --- Trivia Drop Gameplay Endpoints ---

/** Whether the live question takes one answer or all applicable answers. */
export type TriviaAnswerMode = 'single' | 'multiple';

/** One answer option as a player sees it: its stable index and its text. */
export interface TriviaPublicOption {
    option_index: number;
    option_text: string;
}

/**
 * The live question and this player's submission state.
 *
 * `options` are display only and `answer_mode` tells the screen which
 * instruction and controls to render. The correct-option set never travels
 * here -- not the indexes, not even how many of them there are.
 */
export interface TriviaCurrentQuestion {
    question_id: string | null;
    question_text: string | null;
    options: TriviaPublicOption[];
    answer_mode: TriviaAnswerMode | null;
    released_at: string | null;
    submitted: boolean;
    /** `submitted` once answered, `active` while playable. */
    session_status?: string | null;
    /** Sentence the screen shows when no question is live. */
    message?: string | null;
}

/** The confirmation an answered player reads -- and all they read. */
export interface TriviaSubmitResponse {
    question_id: string;
    submitted: boolean;
    submitted_at: string;
    message: string;
}

/** One ranked submission on an ended question's results. */
export interface TriviaLeaderboardEntry {
    rank: number;
    user_id: string;
    display_name: string;
    is_correct: boolean;
    response_time_ms: number;
    submitted_at: string;
    selected_indexes: number[];
}

/**
 * An ended question with its key revealed and its leaderboard settled.
 * Served only once the session has ended; a live question is refused.
 */
export interface TriviaResults {
    question_id: string;
    question_text: string;
    options: TriviaPublicOption[];
    correct_indexes: number[];
    answer_mode: TriviaAnswerMode;
    released_at: string | null;
    ended_at: string | null;
    my_submission: TriviaLeaderboardEntry | null;
    leaderboard: TriviaLeaderboardEntry[];
}

/** The live question and this player's submission state. Never creates a row. */
export const getCurrentTrivia = async (): Promise<TriviaCurrentQuestion> => {
    const response = await axios.get(`${API_URL}/games/trivia-drop`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/**
 * Settle one complete selection against the live question. Exactly one
 * accepted submission per player per question; a multiple-answer selection
 * travels as one complete set in this one request.
 */
export const submitTriviaAnswer = async (
    questionId: string,
    selectedIndexes: number[]
): Promise<TriviaSubmitResponse> => {
    const response = await axios.post(
        `${API_URL}/games/trivia-drop/submit`,
        { question_id: questionId, selected_indexes: selectedIndexes },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** This caller's own settled answer on one question. */
export interface TriviaMySubmission {
    is_correct: boolean;
    response_time_ms: number;
    submitted_at: string;
    selected_indexes: number[];
}

/**
 * The most recently ended question, with its answer key revealed. Empty
 * (`question_id` null) when no question has ended yet.
 */
export interface TriviaPreviousQuestion {
    question_id: string | null;
    question_text: string | null;
    options: TriviaPublicOption[];
    correct_indexes: number[];
    answer_mode: TriviaAnswerMode | null;
    released_at: string | null;
    ended_at: string | null;
    my_submission: TriviaMySubmission | null;
}

/** The most recently ended question with its answer. Empty when none has ended. */
export const getPreviousTrivia = async (): Promise<TriviaPreviousQuestion> => {
    const response = await axios.get(`${API_URL}/games/trivia-drop/previous`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** One ended question's results and leaderboard. Refused while it is live. */
export const getTriviaResults = async (questionId: string): Promise<TriviaResults> => {
    const response = await axios.get(`${API_URL}/games/trivia-drop/results/${questionId}`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

// --- Trivia Drop Configuration Endpoints ---

/** One answer option as stored: its stable index, text, and key membership. */
export interface TriviaOption {
    option_index: number;
    option_text: string;
    is_correct: boolean;
}

/**
 * One Trivia Drop question as the admin screen reads it: text, options, key,
 * derived mode, and lifecycle. `correct_indexes` is present deliberately --
 * the endpoint it comes from is closed to everyone who may not publish.
 */
export interface TriviaQuestion {
    id: string;
    question_text: string;
    options: TriviaOption[];
    correct_indexes: number[];
    answer_mode: TriviaAnswerMode;
    status: string;
    scheduled_for: string | null;
    released_at: string | null;
    ended_at: string | null;
    created_by: string;
    created_at: string;
    updated_at: string;
}

/**
 * The fields `POST /admin/trivia-drop` accepts.
 *
 * `correct_indexes` names the correct options by position; the answer mode is
 * derived from how many are named. `release_mode="scheduled"` needs
 * `scheduled_for` (UTC); `"now"` deploys immediately; `"draft"` stores it.
 */
export interface TriviaQuestionPayload {
    question_text: string;
    options: string[];
    correct_indexes: number[];
    release_mode?: 'now' | 'scheduled' | 'draft';
    scheduled_for?: string;
}

/** Author one question: draft it, schedule it, or deploy it right now. */
export const createTriviaQuestion = async (
    payload: TriviaQuestionPayload
): Promise<TriviaQuestion> => {
    const response = await axios.post(`${API_URL}/admin/trivia-drop`, payload, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/**
 * One page of every Trivia Drop question, newest first, with the total beside it.
 *
 * `status` optionally narrows to a lifecycle subset (comma-separated) so the screen
 * reads its small working set separately from its unbounded history instead of loading
 * the bank to split it by hand. Legacy bare arrays normalise to a single page.
 */
export const getAdminTriviaQuestions = async (params?: {
    skip?: number;
    limit?: number;
    status?: string;
}): Promise<{ questions: TriviaQuestion[]; total: number; skip: number; limit: number }> => {
    const response = await axios.get(`${API_URL}/admin/trivia-drop`, {
        headers: getAuthHeaders(),
        params: {
            ...(params?.skip !== undefined ? { skip: params.skip } : {}),
            ...(params?.limit !== undefined ? { limit: params.limit } : {}),
            ...(params?.status ? { status: params.status } : {}),
        },
    });
    const { rows, total } = normalizeGamesPage<TriviaQuestion>(response.data, 'questions');
    return { questions: rows, total, skip: params?.skip ?? 0, limit: params?.limit ?? rows.length };
};

/** The live Trivia Drop question for the admin screen, or 404 when none is live. */
export const getCurrentAdminTriviaQuestion = async (): Promise<TriviaQuestion> => {
    const response = await axios.get(`${API_URL}/admin/trivia-drop/current`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Revise a draft with no answers. Live and answered questions are immutable. */
export const updateTriviaDraft = async (
    questionId: string,
    payload: { question_text?: string; options?: string[]; correct_indexes?: number[] }
): Promise<TriviaQuestion> => {
    const response = await axios.put(`${API_URL}/admin/trivia-drop/${questionId}`, payload, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Deploy one draft or scheduled question right now, ending any live one. */
export const deployTriviaQuestion = async (questionId: string): Promise<TriviaQuestion> => {
    const response = await axios.post(
        `${API_URL}/admin/trivia-drop/${questionId}/deploy`,
        {},
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Hold one draft for a future release (UTC). The live question is untouched. */
export const scheduleTriviaQuestion = async (
    questionId: string,
    scheduledFor: string
): Promise<TriviaQuestion> => {
    const response = await axios.post(
        `${API_URL}/admin/trivia-drop/${questionId}/schedule`,
        { scheduled_for: scheduledFor },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Move one scheduled question's release to a new future time (UTC). */
export const rescheduleTriviaQuestion = async (
    questionId: string,
    scheduledFor: string
): Promise<TriviaQuestion> => {
    const response = await axios.post(
        `${API_URL}/admin/trivia-drop/${questionId}/reschedule`,
        { scheduled_for: scheduledFor },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Withdraw a draft or scheduled question. History is never rewritten. */
export const cancelTriviaQuestion = async (questionId: string): Promise<TriviaQuestion> => {
    const response = await axios.post(
        `${API_URL}/admin/trivia-drop/${questionId}/cancel`,
        {},
        { headers: getAuthHeaders() }
    );
    return response.data;
};

// --- Reaction Time Gameplay Endpoints ---

/** Whether a submission is still waiting for its result or has been settled. */
export type ReactionTimeStatus = 'pending' | 'valid' | 'too_early';

/**
 * One submission, as the game screen renders it.
 *
 * `reaction_time_ms` is set only on `valid` rows; `elapsed_ms` only on
 * `too_early` rows, where it says how quickly the user clicked after the red
 * state began rather than any kind of score.
 */
export interface ReactionTimeAttempt {
    submission_id: string;
    attempt_number: number;
    status: ReactionTimeStatus;
    reaction_time_ms: number | null;
    elapsed_ms: number | null;
    started_at: string;
    green_at: string | null;
    clicked_at: string | null;
    created_at: string;
}

/**
 * The caller's place in the featured game, as the page opens and after every
 * write. `best_ms` is the minimum valid reaction time and `average_ms` the
 * mean of valid reaction times only; both are null until the caller has a
 * valid attempt. `pending_submission` is the interrupted attempt, if any --
 * it already counts as consumed.
 */
export interface ReactionTimeState {
    game_id: string | null;
    game_date: string | null;
    max_submissions: number;
    submissions_used: number;
    submissions_remaining: number;
    is_finished: boolean;
    attempts: ReactionTimeAttempt[];
    valid_count: number;
    false_start_count: number;
    best_ms: number | null;
    average_ms: number | null;
    pending_submission: ReactionTimeAttempt | null;
}

/** The caller's current state. Never creates anything. */
export const getReactionTimeState = async (): Promise<ReactionTimeState> => {
    const response = await axios.get(`${API_URL}/games/reaction-time`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/**
 * Consume one submission: the blue-screen click that starts an attempt. The
 * pending submission is created here, before the red wait begins, so a false
 * start still spends its submission and a refresh cannot grant another one.
 */
export const startReactionTimeAttempt = async (
    gameId: string
): Promise<ReactionTimeState> => {
    const response = await axios.post(
        `${API_URL}/games/reaction-time/start`,
        { game_id: gameId },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/**
 * Settle a pending attempt with its measured result. `valid` carries the
 * client-measured reaction time (green until click); `too_early` carries only
 * informational elapsed time that is excluded from every statistic.
 */
export const completeReactionTimeAttempt = async (payload: {
    submission_id: string;
    status: 'valid' | 'too_early';
    reaction_time_ms?: number;
    elapsed_ms?: number;
    green_at?: string;
    clicked_at?: string;
}): Promise<ReactionTimeState> => {
    const response = await axios.post(
        `${API_URL}/games/reaction-time/complete`,
        payload,
        { headers: getAuthHeaders() }
    );
    return response.data;
};

// --- Two-Key Escape Gameplay Endpoints ---
//
// The cooperative maze: one shared template per period, one live overlay per
// mutually selected couple. Role clicks stay local until confirmed; the
// confirmations, moves and key uses below are the only writes, and each
// answers the authoritative state so the screen renders what it is given.

/** The two complementary roles. Finalized exactly once per couple game. */
export type TwoKeyEscapeRole = 'navigator' | 'key_operator';

/** One orthogonal step. */
export type TwoKeyEscapeDirection = 'up' | 'down' | 'left' | 'right';

/** Where the caller stands in the couple's maze. */
export type TwoKeyEscapeStage = 'no_couple' | 'selecting_roles' | 'playing' | 'completed';

/** One key: the lock it is spent on. Keys are reusable; the mapping stays hidden. */
export interface TwoKeyEscapeKeyView {
    key_index: number;
    target_row: number;
    target_col: number;
}

/** The settled couple result, finalized exactly once at the exit. */
export interface TwoKeyEscapeResultView {
    completed: boolean;
    total_moves: number;
    total_keys: number;
    /** Seconds from the first valid move to the exit, including time away. */
    elapsed_seconds: number | null;
    completed_at: string | null;
}

/**
 * The caller's place in the maze, as the page opens and after every write.
 * `my_submitted_role` / `partner_submitted_role` carry both confirmed
 * choices so a conflict names itself; `conflict` is the sentence both sides
 * resolve. The board (walls, locked, blocked, player, exit, keys, cooldown)
 * is the same authoritative overlay for both participants.
 */
export interface TwoKeyEscapeState {
    stage: TwoKeyEscapeStage;
    game_id: string | null;
    session_id: string | null;
    /** The caller's pending partner choice, shared with the Questionnaire. */
    my_selection: { selected_user_id: string; selected_user_name: string } | null;
    partner: { id: string; name: string } | null;
    my_role: TwoKeyEscapeRole | null;
    my_submitted_role: TwoKeyEscapeRole | null;
    partner_submitted_role: TwoKeyEscapeRole | null;
    roles_finalized: boolean;
    conflict: string | null;
    grid_size: number | null;
    key_charges: number;
    player_row: number | null;
    player_col: number | null;
    exit_row: number | null;
    exit_col: number | null;
    walls: number[][];
    locked_cells: number[][];
    blocked_cells: number[][];
    keys: TwoKeyEscapeKeyView[];
    cooldown_seconds_remaining: number;
    total_moves: number;
    total_keys: number;
    state_version: number;
    result: TwoKeyEscapeResultView | null;
}

/** One couple's standing in the current period, in rank order. */
export interface TwoKeyEscapeLeaderboardEntry {
    rank: number;
    couple_names: string[];
    completed: boolean;
    total_moves: number;
    total_keys: number;
    elapsed_seconds: number | null;
}

/** The caller's current state. Never creates anything. */
export const getTwoKeyEscapeState = async (): Promise<TwoKeyEscapeState> => {
    const response = await axios.get(`${API_URL}/games/two-key-escape`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/**
 * One page of participants the caller may choose, excluding only themselves. The
 * Questionnaire's own candidate list: settling the pairing here or there
 * chooses from the same people under the same rules, and the page, search and
 * filters are its SQL ones.
 */
export const getTwoKeyEscapePartners = (
    params?: CouplePartnersQuery
): Promise<CouplePartnersPage> =>
    fetchCouplePartners('/games/two-key-escape/partners', params);

/**
 * Record the caller's partner choice through the Questionnaire's selection.
 * A changed mind replaces the previous pick; a mutual pick creates the one
 * session for the pair, visible in both games at once. Answers the caller's
 * escape state, so the screen advances in one round trip.
 */
export const selectTwoKeyEscapePartner = async (
    selectedUserId: string
): Promise<TwoKeyEscapeState> => {
    const response = await axios.post(
        `${API_URL}/games/two-key-escape/selection`,
        { selected_user_id: selectedUserId },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/**
 * Confirm this role choice. Writes only the caller's submission; a distinct
 * pair finalizes atomically, identical choices surface the conflict. Safe to
 * retry once finalized.
 */
export const submitTwoKeyEscapeRole = async (
    role: TwoKeyEscapeRole
): Promise<TwoKeyEscapeState> => {
    const response = await axios.post(
        `${API_URL}/games/two-key-escape/role-submit`,
        { role },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/**
 * Replace this caller's confirmed choice before finalization. Refused once
 * finalized, so the removed control is gone server-side as well.
 */
export const changeTwoKeyEscapeRole = async (
    role: TwoKeyEscapeRole
): Promise<TwoKeyEscapeState> => {
    const response = await axios.post(
        `${API_URL}/games/two-key-escape/role-change`,
        { role },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** Advance the character one cell. Navigator only; refusals change nothing. */
export const moveTwoKeyEscape = async (
    direction: TwoKeyEscapeDirection
): Promise<TwoKeyEscapeState> => {
    const response = await axios.post(
        `${API_URL}/games/two-key-escape/move`,
        { direction },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/**
 * Spend one key charge. Key Operator only; refusals consume nothing and
 * reset nothing.
 */
export const spendTwoKeyEscapeKey = async (
    keyIndex: number
): Promise<TwoKeyEscapeState> => {
    const response = await axios.post(
        `${API_URL}/games/two-key-escape/key`,
        { key_index: keyIndex },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

/** This period's couples in rank order: completed first, then by play. */
export const getTwoKeyEscapeLeaderboard = async (): Promise<TwoKeyEscapeLeaderboardEntry[]> => {
    const response = await axios.get(`${API_URL}/games/two-key-escape/leaderboard`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

// --- League leaderboards ---
//
// Server-calculated league standings across all games. Individual boards
// rank users; couple boards rank canonical pairs; per-game boards rank one
// game's normalized contribution. Scores are never computed client-side:
// finalization writes awards first and these endpoints only query them.

/** The leaderboard subject type: one user, or one canonical pair. */
export type LeagueCompetitionType = 'individual' | 'couple';

/** Daily, weekly, monthly (UTC) or cumulative all-time. */
export type LeaguePeriod = 'daily' | 'weekly' | 'monthly' | 'all_time';

/** One user's standing: rank, normalized league points and native context. */
export interface LeagueIndividualEntry {
    rank: number;
    user_id: string;
    display_name: string;
    score: number;
    native: Record<string, number | boolean | null>;
    is_featured: boolean;
    opportunities_played: number;
    per_game?: Record<string, number>;
}

/** One canonical couple's standing. Member order never duplicates a couple. */
export interface LeagueCoupleEntry {
    rank: number;
    member_a_id: string;
    member_b_id: string;
    member_a_name: string;
    member_b_name: string;
    score: number;
    native: Record<string, number | boolean | null>;
    is_featured: boolean;
    opportunities_played: number;
    per_game?: Record<string, number>;
}

export interface LeagueIndividualBoard {
    competition_type: 'individual';
    period: string;
    game_id?: string | null;
    opportunity_count?: number | null;
    total: number;
    entries: LeagueIndividualEntry[];
}

export interface LeagueCoupleBoard {
    competition_type: 'couple';
    period: string;
    game_id?: string | null;
    opportunity_count?: number | null;
    total: number;
    entries: LeagueCoupleEntry[];
}

export interface LeagueMyStanding {
    competition_type: string;
    period: string;
    entries: Array<LeagueIndividualEntry | LeagueCoupleEntry>;
}

export interface LeagueBoardQuery {
    period?: LeaguePeriod;
    /** YYYY-MM-DD selecting the day/week/month (default: today UTC). */
    anchor?: string;
    /** A game id to narrow to one game's board. */
    gameId?: string;
    skip?: number;
    limit?: number;
}

const leagueParams = (query: LeagueBoardQuery): Record<string, string | number> => {
    const params: Record<string, string | number> = {};
    if (query.period) params.period = query.period;
    if (query.anchor) params.anchor = query.anchor;
    if (query.gameId) params.game_id = query.gameId;
    if (query.skip !== undefined) params.skip = query.skip;
    if (query.limit !== undefined) params.limit = query.limit;
    return params;
};

/** Ranked individual standings: global, or one individual game's board. */
export const getIndividualLeaderboard = async (
    query: LeagueBoardQuery = {}
): Promise<LeagueIndividualBoard> => {
    const response = await axios.get(`${API_URL}/leaderboards/individual`, {
        headers: getAuthHeaders(),
        params: leagueParams(query),
    });
    return response.data;
};

/** Ranked couple standings: global, or one couple game's board. */
export const getCouplesLeaderboard = async (
    query: LeagueBoardQuery = {}
): Promise<LeagueCoupleBoard> => {
    const response = await axios.get(`${API_URL}/leaderboards/couples`, {
        headers: getAuthHeaders(),
        params: leagueParams(query),
    });
    return response.data;
};

/** One game's normalized leaderboard for one period and subject type. */
export const getGameLeaderboard = async (
    gameId: string,
    query: LeagueBoardQuery & { competitionType?: LeagueCompetitionType } = {}
): Promise<LeagueIndividualBoard | LeagueCoupleBoard> => {
    const { competitionType, ...rest } = query;
    const response = await axios.get(`${API_URL}/leaderboards/games/${gameId}`, {
        headers: getAuthHeaders(),
        params: {
            ...leagueParams(rest),
            ...(competitionType ? { competition_type: competitionType } : {}),
        },
    });
    return response.data;
};

/** The caller's own entries on one board, with ranks. */
export const getMyLeagueStanding = async (
    competitionType: LeagueCompetitionType = 'individual',
    query: Omit<LeagueBoardQuery, 'gameId'> = {}
): Promise<LeagueMyStanding> => {
    const response = await axios.get(`${API_URL}/leaderboards/me`, {
        headers: getAuthHeaders(),
        params: {
            ...leagueParams(query),
            competition_type: competitionType,
        },
    });
    return response.data;
};

// --- Per-game leaderboard categories ---
//
// Metric rankings across sessions (solves, attempts, milliseconds, words
// placed, predictions correct). Record-scope categories answer all-time
// history regardless of the rolling window. Nothing here feeds global
// scores; `metrics` keys are documented per category on the component side.

/** One selectable leaderboard view for one game. */
export interface GameCategoryInfo {
    id: string;
    label: string;
    description: string;
    /** 'period': selected UTC window. 'record': all-time personal record. */
    scope: 'period' | 'record';
    is_default: boolean;
}

/** One ranked row: identity plus the category's metrics and detail records. */
export interface GameCategoryEntry {
    rank: number;
    user_id?: string | null;
    display_name?: string | null;
    member_a_id?: string | null;
    member_b_id?: string | null;
    member_a_name?: string | null;
    member_b_name?: string | null;
    metrics: Record<string, number | boolean | string | null>;
    records: Array<Record<string, number | boolean | string | string[] | null>>;
}

export interface GameCategoryBoard {
    game_id: string;
    category: string;
    scope: 'period' | 'record';
    period: string;
    anchor?: string | null;
    total: number;
    entries: GameCategoryEntry[];
}

export interface GameCategoryMe {
    game_id: string;
    category: string;
    scope: 'period' | 'record';
    entries: GameCategoryEntry[];
}

export interface GameCategoryQuery {
    period?: LeaguePeriod;
    anchor?: string;
    skip?: number;
    limit?: number;
}

const gameCategoryParams = (query: GameCategoryQuery): Record<string, string | number> => {
    const params: Record<string, string | number> = {};
    if (query.period) params.period = query.period;
    if (query.anchor) params.anchor = query.anchor;
    if (query.skip !== undefined) params.skip = query.skip;
    if (query.limit !== undefined) params.limit = query.limit;
    return params;
};

/** Selectable leaderboard views for one game. */
export const getGameCategories = async (gameId: string): Promise<GameCategoryInfo[]> => {
    const response = await axios.get(`${API_URL}/leaderboards/games/${gameId}/categories`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** One game's category leaderboard, ranked over the full filtered set. */
export const getGameCategoryBoard = async (
    gameId: string,
    category: string,
    query: GameCategoryQuery = {},
): Promise<GameCategoryBoard> => {
    const response = await axios.get(
        `${API_URL}/leaderboards/games/${gameId}/boards/${category}`,
        { headers: getAuthHeaders(), params: gameCategoryParams(query) },
    );
    return response.data;
};

/** The caller's own rows on one category board, with full-dataset ranks. */
export const getGameCategoryMe = async (
    gameId: string,
    category: string,
    query: GameCategoryQuery = {},
): Promise<GameCategoryMe> => {
    const response = await axios.get(
        `${API_URL}/leaderboards/games/${gameId}/boards/${category}/me`,
        { headers: getAuthHeaders(), params: gameCategoryParams(query) },
    );
    return response.data;
};

/** Selectable leaderboard views for one game, as the console reads them. */
export const getAdminGameCategories = async (gameId: string): Promise<GameCategoryInfo[]> => {
    const response = await axios.get(
        `${API_URL}/admin/leaderboards/games/${gameId}/categories`,
        { headers: getAuthHeaders() },
    );
    return response.data;
};

/** One game's category leaderboard for the console. */
export const getAdminGameCategoryBoard = async (
    gameId: string,
    category: string,
    query: GameCategoryQuery = {},
): Promise<GameCategoryBoard> => {
    const response = await axios.get(
        `${API_URL}/admin/leaderboards/games/${gameId}/boards/${category}`,
        { headers: getAuthHeaders(), params: gameCategoryParams(query) },
    );
    return response.data;
};

/** One game's headline record board with its current top row. */
export interface GameRecordHighlight {
    game_id: string;
    category: string;
    label: string;
    /** 'period': selected UTC window. 'record': all-time record board. */
    scope: 'period' | 'record';
    /** Null when the game has no finalized result yet. */
    entry: GameCategoryEntry | null;
}

export interface GameRecordHighlights {
    period: string;
    highlights: GameRecordHighlight[];
}

/** Every game's headline record (all-time) for the console overview. */
export const getAdminGameRecords = async (): Promise<GameRecordHighlights> => {
    const response = await axios.get(`${API_URL}/admin/leaderboards/records`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/**
 * Ranked individual standings for the games console.
 *
 * The admin mirror of the player-facing board: staff hold no participant
 * row, so the console reads here rather than through `/leaderboards`.
 */
export const getAdminIndividualLeaderboard = async (
    query: LeagueBoardQuery = {}
): Promise<LeagueIndividualBoard> => {
    const response = await axios.get(`${API_URL}/admin/leaderboards/individual`, {
        headers: getAuthHeaders(),
        params: leagueParams(query),
    });
    return response.data;
};

/** Ranked couple standings for the games console. */
export const getAdminCouplesLeaderboard = async (
    query: LeagueBoardQuery = {}
): Promise<LeagueCoupleBoard> => {
    const response = await axios.get(`${API_URL}/admin/leaderboards/couples`, {
        headers: getAuthHeaders(),
        params: leagueParams(query),
    });
    return response.data;
};

/** Finalized opportunities and live awards per game, for operators. */
export const getLeagueSummary = async (): Promise<{
    games: Record<string, { opportunities: number; awards: number }>;
}> => {
    const response = await axios.get(`${API_URL}/admin/leaderboards/summary`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Finalize one closed event's league awards (idempotent repair path). */
export const finalizeLeagueOpportunity = async (
    gameId: string,
    opportunityId: string
): Promise<{
    game_id: string;
    opportunity_id: string;
    action: string;
    awards?: number;
    is_featured?: boolean;
}> => {
    const response = await axios.post(
        `${API_URL}/admin/leaderboards/finalize`,
        { game_id: gameId, opportunity_id: opportunityId },
        { headers: getAuthHeaders() }
    );
    return response.data;
};


/* --- Staff Notifications (admin + moderator) --- */

/** One staff notice: a stored report/love-note row or a live game alert. */
export interface StaffNotification {
    id: string;
    audience: string;
    category: 'report' | 'love_note' | 'game';
    heading: string;
    body: string;
    created_at: string;
    is_read: boolean;
    /** True for computed game alerts (no read state, clear when fixed). */
    is_live: boolean;
    target_kind: string | null;
    target_id: string | null;
}

export interface StaffNotificationPreferences {
    reports_enabled: boolean;
    love_notes_enabled: boolean;
    games_enabled: boolean;
}

export interface StaffNotificationsPage {
    notifications: StaffNotification[];
    total: number;
    skip: number;
    limit: number;
    preferences: StaffNotificationPreferences;
    notifications_enabled: boolean;
    categories: string[];
}

/** One page of this staff account's notices, newest first, with switches beside it. */
export const getStaffNotifications = async (params?: {
    skip?: number;
    limit?: number;
    q?: string;
    category?: string;
    unread_only?: boolean;
}): Promise<StaffNotificationsPage> => {
    const response = await axios.get(`${API_URL}/staff/notifications`, {
        headers: getAuthHeaders(),
        params: {
            ...(params?.skip !== undefined ? { skip: params.skip } : {}),
            ...(params?.limit !== undefined ? { limit: params.limit } : {}),
            ...(params?.q ? { q: params.q } : {}),
            ...(params?.category ? { category: params.category } : {}),
            ...(params?.unread_only ? { unread_only: true } : {}),
        },
    });
    return response.data;
};

export const getStaffUnreadCount = async (): Promise<{
    unread: number;
    by_category: Record<string, number>;
}> => {
    const response = await axios.get(`${API_URL}/staff/notifications/unread-count`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const getStaffNotificationPreferences = async (): Promise<{
    preferences: StaffNotificationPreferences;
    notifications_enabled: boolean;
}> => {
    const response = await axios.get(`${API_URL}/staff/notifications/preferences`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const updateStaffNotificationPreferences = async (payload: Partial<StaffNotificationPreferences>): Promise<{
    preferences: StaffNotificationPreferences;
}> => {
    const response = await axios.put(`${API_URL}/staff/notifications/preferences`, payload, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Flip this staff account's own notifications_enabled (the Enable button). */
export const setStaffNotificationsEnabled = async (enabled: boolean): Promise<{
    notifications_enabled: boolean;
}> => {
    const response = await axios.put(
        `${API_URL}/staff/notifications/enabled`,
        { enabled },
        { headers: getAuthHeaders() }
    );
    return response.data;
};

export const markStaffNotificationRead = async (notificationId: string) => {
    const response = await axios.put(
        `${API_URL}/staff/notifications/${notificationId}/read`,
        {},
        { headers: getAuthHeaders() }
    );
    return response.data;
};
