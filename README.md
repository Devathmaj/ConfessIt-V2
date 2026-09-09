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
- **Database:** PostgreSQL 16 with SQLAlchemy ORM and psycopg
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

2. **Create the local credential files:**

   Both `.env` files are gitignored, so a fresh clone has neither and the stack will refuse to start without them. Copy the values you need into:

   - `.env` (repo root): `POSTGRES_PASSWORD` and `REDIS_PASSWORD`. Docker Compose reads this file to resolve `${VAR}` references in `docker-compose.yml`, injecting them as environment variables when the stack starts. A missing value fails the start-up immediately instead of falling back to a password committed to git.
   - `backend/.env`: application secrets, plus the `REDIS_URL` template. The backend reads this file from `/app` at run time, and python-dotenv substitutes `${REDIS_PASSWORD}` there from its container environment, so the root `.env` stays the only file the Redis password is written in.

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
backend/
  app/
    routers/
    services/
    db_models.py
    postgres.py
  Dockerfile
  requirements.txt
frontend/
  src/
  Dockerfile
  package.json
docker-compose.yml
postgres-init.sql
README.md
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

- `POSTGRES_PASSWORD`: PostgreSQL password for the Compose database. Set it in the root `.env`; Compose requires it and interpolates it into `DATABASE_URL`. The local value is intended for development, so use a strong one for deployments.
- `REDIS_PASSWORD`: Redis password for the Compose cache, defined once in the root `.env`. Compose requires it, passes it to the server as `--requirepass`, and injects it into the backend so the `REDIS_URL` template can resolve it. Redis is not published to the host, so reach it with `docker compose exec redis redis-cli` rather than a local client.
- `REDIS_URL`: Redis connection string template in `backend/.env`, written as `redis://:${REDIS_PASSWORD}@redis:6379` so the password is never duplicated. Use URL-safe characters for `REDIS_PASSWORD`, or percent-encode anything reserved in a URL.
- `DATABASE_URL`: PostgreSQL connection string used by the backend. Compose configures it automatically.
