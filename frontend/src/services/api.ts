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

export const createConfession = async (confession: any) => {
  const response = await axios.post(`${API_URL}/confessions`, confession, { headers: getAuthHeaders() });
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

export const findMatch = async () => {
    const response = await axios.get(`${API_URL}/matchmaking/find`, {
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
export const sendLoveNote = async (noteData: any) => {
    const response = await axios.post(`${API_URL}/love-notes/send`, noteData, { headers: getAuthHeaders() });
    return response.data;
}
