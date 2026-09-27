// src/services/api.ts

import axios from 'axios';
import { endSessionIfTokenWasRejected, isSessionSupersededError } from '@/lib/authEvents';
import type { AccountType } from '@/lib/capabilities';
import type { Cohort, UserGender, UserYear } from '@/lib/accountFields';

const API_URL = 'http://localhost:8001';

// The refresh cookie is an HttpOnly cookie scoped to `Path=/auth`, so the browser only
// attaches it to /auth requests. That is the whole point, but it means the attachment is
// opt-in: every request must send credentials cross-origin or the cookie never travels,
// and /auth/refresh would find no session to extend.
axios.defaults.withCredentials = true;

// Used to get authentication headers from local storage
const getAuthHeaders = () => {
  const token = localStorage.getItem('accessToken');
  return token ? { Authorization: `Bearer ${token}` } : {};
};

/**
 * Ask the server to mint a fresh access token for the session held by the refresh cookie.
 *
 * The cookie itself stays out of sight in page scripts; the server rotates it on every use,
 * so the proven holder of a cookie comes back with a new cookie and a short-lived access
 * token, which is stored where the rest of the app reads it. Any failure — a lapsed or
 * revoked cookie, a superseded session, a network gap — answers null so the caller can end
 * the session instead of looping.
 */
export const refreshAccessToken = async (): Promise<string | null> => {
  try {
    const response = await axios.post(`${API_URL}/auth/refresh`);
    const accessToken = response.data?.access_token;
    if (typeof accessToken !== 'string') return null;
    localStorage.setItem('accessToken', accessToken);
    return accessToken;
  } catch {
    return null;
  }
};

/**
 * Tell the server the session is over, without waiting for it.
 *
 * Signing out must not hang on the network, and the local session is cleared either way; a
 * failed call only means the refresh cookie lapses at its deadline instead of being revoked
 * now, which changes nothing the account can still reach.
 */
export const serverLogout = async (): Promise<void> => {
  try {
    await axios.post(`${API_URL}/auth/logout`);
  } catch {
    // The browser has already forgotten the session; a stale cookie is refused later.
  }
};

// 401s from the auth routes answer on their own terms: a failed login, a stale magic link,
// or a refused refresh must not be mistaken for an access token that needs extending. They
// are passed through untouched, and only they are -- `/auth/me` still gets the retry path.
const isAuthRoute = (url: string | undefined) =>
  !!url && (url.includes('/auth/login/') || url.endsWith('/auth/refresh') || url.endsWith('/auth/logout'));

// One refresh at a time. A page with several requests in flight can fail together, and each
// failure shares this in-flight refresh instead of starting its own: exactly one rotation
// reaches the server, which matters because the server rotates on every use and two
// concurrent refreshes would race that rule.
let refreshInFlight: Promise<string | null> | null = null;

// A request replayed once is not replayed again; a second 401 after a fresh access token is
// authoritative rather than a reason to keep trying. Weak for the request's config object,
// so nothing is held onto after the request completes.
const alreadyRetried = new WeakSet<object>();

/**
 * The one place an access token stops being accepted. A 401 that the refresh cookie can
 * cure is retried once with a fresh token; a 401 that nothing can cure ends the session and
 * lets the auth context explain why.
 *
 * Requests that 401 for any reason other than the access token are passed through untouched
 * for their own callers, exactly as they were before the refresh existed.
 */
export const handleResponseError = async (error: any): Promise<any> => {
  const status = error?.response?.status;
  const original = error?.config;
  const url = typeof original?.url === 'string' ? original.url : undefined;

  if (status !== 401 || !original || isAuthRoute(url)) {
    return Promise.reject(error);
  }

  // A request that already rode a fresh access token and still failed is over; the retry
  // did not save it, so the session is not worth keeping.
  if (alreadyRetried.has(original)) {
    endSessionIfTokenWasRejected(error);
    return Promise.reject(error);
  }

  // A 401 from a request that never carried an access token is not a sign-out.
  if (!localStorage.getItem('accessToken')) {
    return Promise.reject(error);
  }

  // A superseded session is over regardless of refresh: the newer login owns the account
  // now, and extending the old session would fight the single-session rule. The dedicated
  // event carries the server's explanation to the "signed in elsewhere" toast.
  if (isSessionSupersededError(error)) {
    endSessionIfTokenWasRejected(error);
    return Promise.reject(error);
  }

  if (!refreshInFlight) {
    refreshInFlight = refreshAccessToken().finally(() => {
      refreshInFlight = null;
    });
  }
  const freshToken = await refreshInFlight;
  if (!freshToken) {
    endSessionIfTokenWasRejected(error);
    return Promise.reject(error);
  }

  alreadyRetried.add(original);
  if (original.headers) {
    original.headers.Authorization = `Bearer ${freshToken}`;
  } else {
    original.headers = { Authorization: `Bearer ${freshToken}` };
  }
  return axios(original);
};

// Registered on the shared instance because every endpoint in this file uses it, so this is
// the one place an expired token is noticed and a stale one is refreshed. Requests that fail
// for any other reason are passed through untouched for their own caller to handle.
axios.interceptors.response.use(
  (response) => response,
  (error) => handleResponseError(error)
);

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

  const response = await axios.get(`${API_URL}/auth/me`, { headers });
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

    const response = await axios.post(`${API_URL}/profile/upload-picture`, formData, {
        headers: {
            ...getAuthHeaders(),
            'Content-Type': 'multipart/form-data',
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

// Used to get all users for recipient selection
export const getAllUsers = async () => {
    const response = await axios.get(`${API_URL}/love-notes/users`, { headers: getAuthHeaders() });
    return response.data;
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

// Used to get received love notes (accepted)
export const getReceivedLoveNotes = async () => {
    const response = await axios.get(`${API_URL}/love-notes/inbox`, { headers: getAuthHeaders() });
    return response.data;
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

export const getNotifications = async () => {
    const response = await axios.get(`${API_URL}/notifications`, { headers: getAuthHeaders() });
    return response.data;
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
    status: AdminReportResolution
): Promise<AdminReportResolutionResult> => {
    const response = await axios.patch(
        `${API_URL}/admin/messages/reports/${reportId}`,
        {},
        { headers: getAuthHeaders(), params: { status } }
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
        const token = localStorage.getItem('accessToken');
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

/** Every participant the caller may choose, excluding only themselves. */
export const getCouplePartners = async (): Promise<CouplePartnerCandidate[]> => {
    const response = await axios.get(`${API_URL}/games/couple-questionnaire/partners`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

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
}

/** Every game's configuration. */
export const getGameConfigs = async (): Promise<GameConfig[]> => {
    const response = await axios.get(`${API_URL}/admin/games/config`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

/** Patch one game's availability, content mode, reset cadence or grid size. */
export const updateGameConfig = async (
    gameId: string,
    payload: Partial<Pick<GameConfig, 'enabled' | 'is_hidden' | 'content_mode' | 'reset_cadence' | 'grid_size'>>
): Promise<GameConfig> => {
    const response = await axios.put(`${API_URL}/admin/games/config/${gameId}`, payload, {
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
 * Every participant the caller may choose, excluding only themselves. The
 * Questionnaire's own candidate list: settling the pairing here or there
 * chooses from the same people under the same rules.
 */
export const getTwoKeyEscapePartners = async (): Promise<CouplePartnerCandidate[]> => {
    const response = await axios.get(`${API_URL}/games/two-key-escape/partners`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

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

