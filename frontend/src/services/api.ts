// src/services/api.ts

import axios from 'axios';
import { endSessionIfTokenWasRejected } from '@/lib/authEvents';
import type { AccountType } from '@/lib/capabilities';
import type { Cohort, UserGender, UserYear } from '@/lib/accountFields';

const API_URL = 'http://localhost:8001';

// Used to get authentication headers from local storage
const getAuthHeaders = () => {
  const token = localStorage.getItem('accessToken');
  return token ? { Authorization: `Bearer ${token}` } : {};
};

// Registered on the shared instance because every endpoint in this file uses it, so this
// is the one place an expired token is noticed. Requests that fail for any other reason
// are passed through untouched for their own caller to handle.
axios.interceptors.response.use(
  (response) => response,
  (error) => {
    endSessionIfTokenWasRejected(error?.response?.status);
    return Promise.reject(error);
  }
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
 * itself -- picture, emoji, bio, interests, and the four preference flags -- is absent, and the
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

