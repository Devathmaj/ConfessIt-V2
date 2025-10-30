// src/services/api.ts

import axios from 'axios';

const API_URL = 'http://localhost:8001';

// Used to get authentication headers from local storage
const getAuthHeaders = () => {
  const token = localStorage.getItem('accessToken');
  return token ? { Authorization: `Bearer ${token}` } : {};
};

// --- Confession Endpoints ---

export const getConfessions = async (sortBy = 'popularity') => {
  const response = await axios.get(`${API_URL}/confessions?sort_by=${sortBy}`, { headers: getAuthHeaders() });
  return response.data;
};

export const getTotalConfessionsCount = async () => {
    const response = await axios.get(`${API_URL}/confessions/total_count`, { headers: getAuthHeaders() });
    return response.data;
};

export const createConfession = async (confession: any) => {
  const response = await axios.post(`${API_URL}/confessions`, confession, { headers: getAuthHeaders() });
  return response.data;
};

export const updateConfession = async (confessionId: string, updateData: { is_anonymous?: boolean; is_comment?: boolean }) => {
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

export const createComment = async (confessionId: string, message: string) => {
  const response = await axios.post(
    `${API_URL}/confessions/${confessionId}/comment`,
    { message: message }, 
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
export const requestConversation = async (matchId: string) => {
    console.log("API: requestConversation called with matchId:", matchId); // Debug log
    const response = await axios.post(`${API_URL}/conversations/request`, { matchId }, {
        headers: getAuthHeaders(),
    });
    console.log("API: requestConversation response:", response.data); // Debug log
    return response.data;
};

export const getConversationStatus = async (matchId: string) => {
    const response = await axios.get(`${API_URL}/conversations/${matchId}/status`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const acceptConversation = async (matchId: string) => {
    const response = await axios.post(`${API_URL}/conversations/${matchId}/accept`, {}, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const rejectConversation = async (matchId: string) => {
    const response = await axios.post(`${API_URL}/conversations/${matchId}/reject`, {}, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const sendMessage = async (matchId: string, text: string) => {
    const response = await axios.post(`${API_URL}/conversations/${matchId}/messages`, { text }, {
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

export const getConversationByMatch = async (matchId: string) => {
    const response = await axios.get(`${API_URL}/conversations/${matchId}`, {
        headers: getAuthHeaders(),
    });
    return response.data;
};

export const getConversationMessages = async (matchId: string) => {
    const response = await axios.get(`${API_URL}/conversations/${matchId}/messages`, {
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

// Used to get all unique classes for filtering
export const getClasses = async () => {
    const response = await axios.get(`${API_URL}/love-notes/classes`, { headers: getAuthHeaders() });
    return response.data;
};

// Used to send a love note for review
export const sendLoveNote = async (noteData: {
    recipient_id: string;
    image_base64: string;
    message_text: string;
    is_anonymous: boolean;
}) => {
    const response = await axios.post(`${API_URL}/love-notes/send`, noteData, { headers: getAuthHeaders() });
    return response.data;
}

// --- Profile Endpoints ---

export const getNotifications = async () => {
    const response = await axios.get(`${API_URL}/notifications`, { headers: getAuthHeaders() });
    return response.data;
};

// --- Admin Endpoints ---

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

export const getAdminUsers = async () => {
    const response = await axios.get(`${API_URL}/admin/users`, { headers: getAuthHeaders() });
    return response.data;
};

export const createAdminUser = async (payload: Record<string, unknown>) => {
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

export const updateAdminUser = async (userId: string, payload: Record<string, unknown>) => {
    const response = await axios.put(
        `${API_URL}/admin/users/${userId}`,
        payload,
        { headers: getAuthHeaders() }
    );
    return response.data;
};

export const getAdminMatchmaking = async () => {
    const response = await axios.get(`${API_URL}/admin/matchmaking`, { headers: getAuthHeaders() });
    return response.data;
};

export const updateAdminMatchmakingStatus = async (
    matchId: string,
    status: 'approved' | 'rejected' | 'pending'
) => {
    const response = await axios.put(
        `${API_URL}/admin/matchmaking/${matchId}/status`,
        {},
        {
            headers: getAuthHeaders(),
            params: { status },
        }
    );
    return response.data;
};