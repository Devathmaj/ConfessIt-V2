# PostgreSQL Data Model

This document describes the active schema defined in `backend/app/db_models.py`. SQLAlchemy models, service queries, API payloads, and frontend types use the same snake_case names. Primary keys are UUIDs exposed as `id`; timestamps use timezone-aware PostgreSQL `TIMESTAMPTZ` and API ISO 8601 values.

## Tables

| Table | Purpose and principal fields |
|---|---|
| `users` | User identity and preferences. `id` UUID primary key; `regno` unique registration key; `name`, `email`, `class_name`, `profile_picture_url`, `role`, boolean preference fields, and typed login/matchmaking timestamps. |
| `confessions` | Posts with `body`, optional `recipient_name`, `anonymous`, `comments_enabled`, `author_id`, `created_at`, moderation state, JSONB reactions, and integer reaction/comment counters. |
| `confession_comments` | `confession_id`, commenter `user_id`, `body`, `created_at`, UUID arrays for likes/dislikes, and counters. |
| `reports` | Confession or comment reports. `confession_id` and `comment_id` are nullable UUID foreign keys with a check requiring exactly one; `reported_by_id`, reporter snapshot, reason, and `created_at`. |
| `login_tokens` | Hashed token, user foreign key, issuance/expiry/consumption timestamps, request metadata, status, and attempt count. Network addresses use PostgreSQL `INET`. |
| `love_notes` | Sender and recipient UUID foreign keys, `image_url`, `body`, `anonymous`, moderation status, `created_at`, and optional `read_at`. |
| `love_note_templates` | Template `name`, `image_url`, `description`, and `created_at`. |
| `matches` | Two user registration keys with `created_at` and `expires_at`; constraint prevents matching a user with themself. |
| `conversations` | Unique `match_id`, participant registration keys, status and lifecycle timestamps, `last_message_preview`, blocked participant registration keys. |
| `messages` | `conversation_id`, sender/receiver registration keys, `body`, `created_at`, and `is_read`. |
| `notifications` | `recipient_regno`, `heading`, `body`, `created_at`, and `is_read`. |
| `message_reports` | Message and reporter/reported registration keys, reason, status, report/review timestamps, and optional reviewer registration key. |

## Conventions

- SQL identifiers and API fields use lowercase `snake_case` consistently.
- UUID relationships use UUID columns and explicit foreign keys. User-facing registration identifiers remain `regno` where the relationship is intentionally based on the registration key.
- Date/time values are stored as timezone-aware PostgreSQL timestamps and serialized as ISO 8601 strings at the API boundary.
- Collections use PostgreSQL arrays when elements have one type; JSONB is limited to the variable reaction map.
- `backend/app/postgres.py` only creates the SQLAlchemy engine and sessions. Application reads and writes use native SQLAlchemy selects and ORM records.
- The application creates current tables from `Base.metadata`; existing data is not migrated.
