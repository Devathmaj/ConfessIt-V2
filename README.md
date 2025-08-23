# ConfessIt

ConfessIt is a full-stack web application that provides a safe and anonymous space for users to share confessions, connect with others, and engage in fun activities. It features anonymous confessions, matchmaking, love notes, and mini-games.

## Features

- **Anonymous Confessions:** Share your thoughts and feelings without revealing your identity.
- **Matchmaking:** Find and connect with other users on the platform.
- **Love Notes:** Send and receive anonymous love notes.
- **Mini-Games:** Play fun games to break the ice.
- **User Profiles:** Customize your profile with a bio, interests, and more.
- **Authentication:** Secure authentication with magic links.

## Tech Stack

- **Frontend:** React, TypeScript, Tailwind CSS, Shadcn UI
- **Backend:** FastAPI, Python
- **Database:** MongoDB
- **Containerization:** Docker, Docker Compose

## Prerequisites

Before you begin, ensure you have the following installed:

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)

## Getting Started

1. **Clone the repository:**

   ```bash
   git clone https://github.com/your-username/confessit.git
   cd confessit
   ```

2. **Create environment files:**

   Create a `db_password.txt` file in the root directory and add your MongoDB password to it:

   ```
   your_mongodb_password
   ```

3. **Build and run the application with Docker Compose:**

   ```bash
   docker-compose up --build
   ```

   This command will build the Docker images for the frontend and backend services and start the containers.

4. **Access the application:**

   - **Frontend:** Open your browser and navigate to `http://localhost:5173`
   - **Backend API:** The API will be running at `http://localhost:8001`. You can access the API documentation at `http://localhost:8001/docs`.

## Project Structure

```
├── backend
│   ├── app
│   │   ├── routers
│   │   ├── services
│   │   └── ...
│   ├── Dockerfile
│   └── requirements.txt
├── frontend
│   ├── src
│   │   ├── components
│   │   ├── pages
│   │   └── ...
│   ├── Dockerfile
│   └── package.json
├── .env
├── db_password.txt
├── docker-compose.yml
└── README.md
```

## API Endpoints

The backend API provides the following endpoints:

- `/auth/login/magic`: Generate a magic link for authentication.
- `/auth/login/magic/verify`: Verify a magic link token.
- `/auth/me`: Get the current user's details.
- `/profile/update`: Update the current user's profile.
- `/profile/upload-picture`: Upload a profile picture.
- `/matchmaking/potential-matches`: Get a list of potential matches.
- `/matchmaking/find`: Find a random match.
- `/confessions`: Create and get confessions.
- `/confessions/{confession_id}/react`: React to a confession.
- `/confessions/{confession_id}/comment`: Comment on a confession.
- `/comments/{comment_id}/like`: Like a comment.
- `/comments/{comment_id}/dislike`: Dislike a comment.

For more details, see the API documentation at `http://localhost:8001/docs`.

## Environment Variables

- `MONGO_PASSWORD`: The password for the MongoDB database.
