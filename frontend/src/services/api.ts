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

export const getConfessions = async (sortBy = 'time') => {
  const response = await axios.get(`${API_URL}/confessions?sort_by=${sortBy}`, { headers: getAuthHeaders() });
  return response.data;
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

export const getAdminConfessions = async () => {
    const response = await axios.get(`${API_URL}/admin/confessions`, { headers: getAuthHeaders() });
    return response.data;
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

export const getAdminLoveNotes = async () => {
    const response = await axios.get(`${API_URL}/admin/love-notes`, { headers: getAuthHeaders() });
    return response.data;
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

export const getAdminLoveNoteReports = async (status?: string, limit?: number) => {
    const response = await axios.get(`${API_URL}/admin/love-notes/reports`, {
        headers: getAuthHeaders(),
        params: { status, limit },
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
// nothing in common but a reason and a status. Every one of them takes the same `status`
// filter and answers with the same { reports, total, skip, limit } envelope, so the screen can
// treat them uniformly without the server pretending they are the same data.
//
// `limit` is for a caller that wants `total` and nothing else -- the moderator dashboard's
// count cards ask for one row and read the envelope's count, rather than downloading fifty
// reported messages to add up a number the query already returned. It is a server-side filter
// and not a display length, so `total` still counts every matching row however few came back.

export const getAdminConfessionReports = async (status?: string, limit?: number) => {
    const response = await axios.get(`${API_URL}/admin/confessions/reports`, {
        headers: getAuthHeaders(),
        params: { status, limit },
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

export const getAdminMessageReports = async (status?: string, limit?: number) => {
    const response = await axios.get(`${API_URL}/admin/messages/reports`, {
        headers: getAuthHeaders(),
        params: { status, limit },
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

export const getAdminUsers = async () => {
    const response = await axios.get(`${API_URL}/admin/users`, { headers: getAuthHeaders() });
    return response.data;
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
    answer: string;
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

/** Every configured puzzle, newest day first. */
export const getAdminWordleGames = async (): Promise<WordleGame[]> => {
    const response = await axios.get(`${API_URL}/admin/wordle`, { headers: getAuthHeaders() });
    return response.data;
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
    answer: string;
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

/** Every configured puzzle, newest day first. */
export const getAdminAnagramGames = async (): Promise<AnagramGame[]> => {
    const response = await axios.get(`${API_URL}/admin/anagram`, { headers: getAuthHeaders() });
    return response.data;
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

