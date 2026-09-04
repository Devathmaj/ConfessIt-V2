# Data Handling Report — ConfessIt V2

> Auto-generated analysis of all database tables, schemas, foreign keys, data models, and data-handling patterns.
> **This document reflects the current working-tree state of the code (NOT the last commit).** The working tree contains a large, uncommitted schema normalization versus `HEAD`.
>
> Source of truth:
> - `backend/app/db_models.py` — SQLAlchemy schema (12 tables, normalized)
> - `backend/app/models.py` — Pydantic models (API contracts)
> - `backend/app/postgres.py` — persistence / repository layer
> - `backend/app/services/*` — business logic and data flows

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Database Tables — Detailed Reference](#2-database-tables--detailed-reference)
   - [2.1 users](#21-users)
   - [2.2 confessions](#22-confessions)
   - [2.3 confession_comments](#23-confession_comments)
   - [2.4 reports](#24-reports)
   - [2.5 login_tokens](#25-login_tokens)
   - [2.6 love_notes](#26-love_notes)
   - [2.7 love_note_templates](#27-love_note_templates)
   - [2.8 matches](#28-matches)
   - [2.9 conversations](#29-conversations)
   - [2.10 messages](#210-messages)
   - [2.11 notifications](#211-notifications)
   - [2.12 message_reports](#212-message_reports)
3. [Foreign Key Map](#3-foreign-key-map)
4. [Pydantic Data Models](#4-pydantic-data-models)
5. [Repository & Data Access Layer](#5-repository--data-access-layer)
6. [Service Layer Data Handling](#6-service-layer-data-handling)
7. [Indexes](#7-indexes)
8. [Data Flow Summary](#8-data-flow-summary)
9. [Data Integrity, Security & Known Issues](#9-data-integrity-security--known-issues)

---

## 1. Architecture Overview

| Layer | Technology | File(s) |
|---|---|---|
| Database | PostgreSQL (native `UUID` keys, `ARRAY`, `INET`, one `JSONB` column) | — |
| ORM / Schema | SQLAlchemy 2.0 (`DeclarativeBase`, `Mapped`, `mapped_column`) | `backend/app/db_models.py` |
| Persistence helper | Custom `TableRepository` exposing a MongoDB-style query/update API over SQLAlchemy | `backend/app/postgres.py` |
| API contracts / validation | Pydantic `BaseModel` | `backend/app/models.py` |
| Config | `pydantic-settings` (`DATABASE_URL`) | `backend/app/config.py` |
| Table creation | `Base.metadata.create_all(engine)` in `PostgresClient.initialize()` | `backend/app/postgres.py:326` |
| Connection pool | `create_engine(url, pool_pre_ping=True)` + `sessionmaker(expire_on_commit=False)` | `backend/app/postgres.py:321-322` |

**Dual-model pattern.** Every entity has two representations:
- **SQLAlchemy record** (`db_models.py`) — persisted state. DB column names are explicit and **snake_case** (`mapped_column("db_name", ...)`); Python attribute names keep the legacy camelCase/PascalCase keys so services barely changed.
- **Pydantic model** (`models.py`) — request validation and API response body.

The repository layer (`postgres.py`) is the translation membrane between the two: it accepts legacy field names in queries/inserts and shims them onto the new normalized columns, and converts ORM rows back into the old-shaped dicts on read.

**ID convention.** All primary keys are PostgreSQL `UUID` columns (`Uuid(as_uuid=False)`), defaulting to dashed UUID strings (`str(uuid.uuid4())` via `_uuid()` / `new_id()`). Pydantic models expose the field as `_id` (alias), the repository translates `_id` → column `id` (`postgres.py:74,86`).

**Table registration.** All 12 models are registered in the `_TABLES` mapping (`postgres.py:55-68`) that drives `db.<name>` attribute access and `clear_all()`.

**Migration status.** The working tree normalizes the schema (removes most `JSONB` blobs, real FKs with `CASCADE`). There are still **no Alembic migrations**; schema is created via `create_all`.

---

## 2. Database Tables — Detailed Reference

### 2.1 `users`

**SQLAlchemy class:** `UserRecord` · **Pydantic models:** `UserDetails`, `AdminUserUpdate` · **Pydantic input:** `AdminUserCreate` · **Accessed by:** nearly every service.

The central table — one row per registered user (students and admins).

| Column | Type | Constraints / Defaults | Purpose |
|---|---|---|---|
| `id` | `UUID` | **PK**, default `_uuid()` | UUID identifier |
| `regno` (attr `Regno`) | `String(100)` | **UNIQUE, indexed** | Registration number; natural key carried in the JWT `sub` claim and used as FK target by matches/conversations/messages/notifications |
| `name` (attr `Name`) | `String(160)` | NOT NULL | Full display name (used as `name` in denormalized `user_info`) |
| `email` | `String(320)` | **UNIQUE, indexed** | Login/identity email |
| `emoji` | `String(16)` | NULL | Avatar emoji (used as `avatar` in denormalized `user_info`) |
| `bio` | `String(500)` | NULL | Free-text bio (was `Text`, now length-capped) |
| `class_name` (attr `which_class`) | `String(80)` | NOT NULL, indexed | Academic year/class (e.g. `"1st year"`) — used for matchmaking filtering |
| `profile_picture_url` (attr `profile_picture_id`) | `Text` | NULL | Cloudinary image identifier/URL |
| `gender` | `String(24)` | NOT NULL | `"male"` / `"female"` — drives matchmaking pairing |
| `interests` | `ARRAY(String(80))` | default `{}`, server default | List of interest strings (was `JSONB`) |
| `matchmaking_enabled` (attr `isMatchmaking`) | `Boolean` | default `true` | Opt-in to matchmaking pool |
| `notifications_enabled` (attr `isNotifications`) | `Boolean` | default `true` | Notification opt-in |
| `love_notes_receive_enabled` (attr `isLovenotesRecieve`) | `Boolean` | default `true` | Can receive love notes |
| `love_notes_send_enabled` (attr `isLovenotesSend`) | `Boolean` | default `false` | Can send love notes |
| `is_blocked` | `Boolean` | default `false` | Admin soft-ban flag |
| `reported_count` | `Integer` | default `0` | Aggregate reports against this user |
| `last_login_at` (attr `last_login_time`) | `DateTime(tz)` | NULL | Last successful magic-link login |
| `last_login_ip` | `INET` (postgres) | NULL | Client IP of last login (native inet type) |
| `last_login_user_agent` | `Text` | NULL | User-agent of last login |
| `role` (attr `user_role`) | `String(20)` | default `"user"`, indexed | `"user"` or `"admin"`; enforced by `admin_required()` (`auth_service.py:112-121`) |
| `last_matchmaking_at` (attr `last_matchmaking_time`) | `DateTime(tz)` | NULL | Throttles how often a user can be matchmade |

**Design notes:**
- **`username` column was removed** in the working tree — `name` is used everywhere instead.
- Auth is **magic-link based**; JWT carries `sub = Regno` (`auth_service.py:62`, `87`, `104`).
- `Regno` is the FK target for matches / conversations / messages / notifications / message_reports, while `users.id` is the FK target for love_notes, confessions, comments, reports, and login_tokens (see section 3).
- `_dict()` casts `last_login_ip` (INET) to `str` on read (`postgres.py:87-88`).
- Seeded by `backend/seed_db.py` with synthetic users (`MA*` / `FE*` registration numbers).

---

### 2.2 `confessions`

**SQLAlchemy class:** `ConfessionRecord` · **Pydantic models:** `Confession`, `ConfessionCreate`, `ConfessionUpdate` · **Service:** `ConfessionService`.

The core content table — a confession post (optionally anonymous) with denormalized reaction counters and moderation state.

| Column | Type | Constraints / Defaults | Purpose |
|---|---|---|---|
| `id` | `UUID` | **PK**, default `_uuid()` | UUID |
| `body` (attr `confession`) | `Text` | NOT NULL | The confession body text |
| `recipient_name` (attr `confessing_to`) | `String(160)` | NULL | Name of the person the confession is addressed to (validated non-empty on create) |
| `anonymous` (attr `is_anonymous`) | `Boolean` | default `false` | Hides author; can only toggle **True → False** (rule in `confession_service.py:180-187`) |
| `comments_enabled` (attr `is_comment`) | `Boolean` | default `false` | Whether others may comment |
| `created_at` (attr `timestamp`) | `DateTime(tz)` | NOT NULL, indexed | Creation time |
| `author_id` (attr `user_id`) | `UUID` | **FK → `users.id` ON DELETE CASCADE**, indexed | Author (now a real FK, removed the soft `user_id` ref) |
| `approved` (attr `is_approved`) | `Boolean` | default `true`, indexed | Moderation swallow flag — admin can hide |
| `report_count` | `Integer` | default `0` | Reports against this confession |
| `reactions` | `JSONB` | default `{}::jsonb` | **The only remaining JSONB blob**: `{ "heart": [userIds], "haha": [...], "whoa": [...], "heartbreak": [...] }` |
| `heart_count` | `Integer` | default `0` | Denormalized `len(reactions.heart)` |
| `haha_count` | `Integer` | default `0` | Denormalized count |
| `whoa_count` | `Integer` | default `0` | Denormalized count |
| `heartbreak_count` | `Integer` | default `0` | Denormalized count |
| `comment_count` | `Integer` | default `0` | Maintained via `$inc` on comment insert |

**Design notes:**
- **Removed columns vs older state:** `user_info` JSONB, `reported_by` JSONB, and the legacy `comments` JSONB column are gone.
- **`reported_by` / `report_count` are now derived at read time** by querying the `reports` table (reporters for this confession) — `ConfessionService._reporter_ids()` (`confession_service.py:21-22,59-60,123-124`). The denormalized counters are not stored on confessions anymore.
- Reactions remain as a JSONB dict of user-ID arrays; user's current reaction is computed by scanning arrays (`confession_service.py:108-114, 217-228`).
- Sorting supported: `popularity` (reaction sum), `comments` (`comment_count`), `time` (`created_at`).

---

### 2.3 `confession_comments`

**SQLAlchemy class:** `ConfessionCommentRecord` · **Pydantic model:** `ConfessionComment` · **Service:** `ConfessionService`.

Normalized child table of comments on confessions.

| Column | Type | Constraints / Defaults | Purpose |
|---|---|---|---|
| `id` | `UUID` | **PK**, default `_uuid()` | UUID |
| `confession_id` | `UUID` | **FK → `confessions.id` ON DELETE CASCADE**, indexed | Parent confession |
| `user_id` | `UUID` | **FK → `users.id` ON DELETE CASCADE**, indexed | Commenter (replaces the old `user_info` JSONB blob) |
| `body` (attr `message`) | `Text` | NOT NULL | Comment text |
| `created_at` (attr `timestamp`) | `DateTime(tz)` | NOT NULL, indexed | Creation time |
| `likes` | `ARRAY(UUID)` | default `{}` | User-IDs who liked |
| `dislikes` | `ARRAY(UUID)` | default `{}` | User-IDs who disliked |
| `like_count` | `Integer` | default `0` | Denormalized `len(likes)` |
| `dislike_count` | `Integer` | default `0` | Denormalized `len(dislikes)` |
| `report_count` | `Integer` | default `0` | Reports (derived on read, same as confessions) |

**Design notes:**
- **`user_info` is reconstructed on read** by the repository: `TableRepository.list()` batches the comment `user_id`s, selects the matching `users`, and builds `user_info = {id, name, avatar}` (`postgres.py:176-186`).
- On writes, `add()` maps `user_info.id` → `user_id` and drops the view field `user_reaction` (`postgres.py:202-205`).
- Queries for `{"user_info.id": ...}` are transparently rewired to the `user_id` column (`postgres.py:114-115`) — used for the **3-comments-per-user limit** (`confession_service.py:251-255`).
- Likes/dislikes remain **mutually exclusive** (`confession_service.py:298-313`).

---

### 2.4 `reports`

**SQLAlchemy class:** `ReportRecord` · **Pydantic model:** `Report` · **Service:** `ConfessionService` / `AdminService`.

Polymorphic reports against confessions **or** comments. The biggest normalization: one polymorphic `content_id`/`content_type` pair became **two nullable FK columns with a check constraint**.

| Column | Type | Constraints / Defaults | Purpose |
|---|---|---|---|
| `id` | `UUID` | **PK**, default `_uuid()` | UUID |
| `confession_id` | `UUID` | NULL, **FK → `confessions.id` ON DELETE CASCADE**, indexed | Target (confession case) |
| `comment_id` | `UUID` | NULL, **FK → `confession_comments.id` ON DELETE CASCADE**, indexed | Target (comment case) |
| `reported_by_id` | `UUID` | **FK → `users.id` ON DELETE CASCADE**, indexed | Reporting user |
| `reported_by_name` | `String(160)` | NOT NULL | Denormalized reporter display name at report time |
| `reason` | `String(1000)` | NOT NULL | Free-text reason (length-capped) |
| `created_at` (attr `timestamp`) | `DateTime(tz)` | NOT NULL, indexed | When filed |

**Table-level constraints:**
- `CheckConstraint("(confession_id IS NOT NULL) <> (comment_id IS NOT NULL)", name="ck_reports_one_target")` — **exactly one target is required**.
- `UniqueConstraint("confession_id", "reported_by_id", name="uq_reports_confession_reporter")` — one report per user per confession.
- `UniqueConstraint("comment_id", "reported_by_id", name="uq_reports_comment_reporter")` — one report per user per comment.

**Compatibility shims (repository):**
- **Write:** `add()` splits `content_id` + `content_type` into `confession_id` or `comment_id` (`postgres.py:206-210`).
- **Read:** `_dict()` re-emits `content_type` (`"comment"` / `"confession"`) and `content_id` for the service layer (`postgres.py:93-95`).
- **Query:** filters on `content_id` are rewritten to `OR(confession_id IN (...), comment_id IN (...))` (`postgres.py:110-113`).

**Design notes:** Reporting a confession/comment now **no longer writes `reported_by` arrays** on the target row — it only inserts a `reports` row and bumps a denormalized counter; the `reported_by` list is derived on read (`confession_service.py:341-390`). Dedupe is enforced by the DB unique constraints *and* a pre-check in the service (`confession_service.py:347, 375`).

---

### 2.5 `login_tokens`

**SQLAlchemy class:** `LoginTokenRecord` · **Pydantic model:** `LoginToken` · **Service:** `AuthService` / `create_token.py`.

Stores issued magic-link tokens for passwordless authentication. **Only the SHA-256 hash of the token is persisted.**

| Column | Type | Constraints / Defaults | Purpose |
|---|---|---|---|
| `id` | `UUID` | **PK**, default `_uuid()` | UUID |
| `user_id` | `UUID` | **FK → `users.id` ON DELETE CASCADE**, indexed | Target user (now a real FK) |
| `token_hash` | `String(64)` | **UNIQUE, indexed** | SHA-256 hex digest (64 chars — corrected from 128) |
| `issued_at` | `DateTime(tz)` | NOT NULL | Issuance |
| `expires_at` | `DateTime(tz)` | NOT NULL, indexed | Expiry (1 hour, see `create_token.py:31`) |
| `consumed_at` | `DateTime(tz)` | NULL | When exchanged |
| `used` | `Boolean` | default `false` | Single-use flag |
| `revoked` | `Boolean` | default `false` | Admin revocation |
| `request_ip` | `INET` | NOT NULL | IP that requested the link |
| `request_user_agent` | `String(512)` | NOT NULL | UA that requested the link |
| `consume_ip` | `INET` | NULL | IP that redeemed the link |
| `consume_user_agent` | `String(512)` | NULL | UA that redeemed the link |
| `attempt_count` | `SmallInteger` | default `0` | Redemption attempts |
| `session_status` | `String(16)` | default `"issued"`, indexed | **Replaces the old `metadata` JSONB** — session state (`"issued"` → `"active"`) |
| `last_seen_at` | `DateTime(tz)` | NULL | **Replaces `metadata.last_seen`** — last session activity |
| `device` | `String(512)` | NULL | **Replaces `metadata.device`** — UA string of the consuming device |

**Security-critical behavior (unchanged):**
- Token is **single-use**: redemption is atomic via `get_and_update` with `SELECT ... FOR UPDATE` (`auth_service.py:158-172`).
- **IP binding**: redemption only allowed from the requesting IP (`auth_service.py:178-179`).
- `last_seen_at` / `device` / `session_status="active"` are written on successful login (`auth_service.py:200-211`).

---

### 2.6 `love_notes`

**SQLAlchemy class:** `LoveNoteRecord` · **Pydantic model:** `LoveNote` · **Service:** `LoveNoteService` / `AdminLoveNotes`.

| Column | Type | Constraints / Defaults | Purpose |
|---|---|---|---|
| `id` | `UUID` | **PK**, default `_uuid()` | UUID |
| `sender_id` | `UUID` | **FK → `users.id` ON DELETE CASCADE**, indexed | Sending user |
| `recipient_id` | `UUID` | **FK → `users.id` ON DELETE CASCADE**, indexed | Receiving user |
| `image_url` | `Text` | NOT NULL | **Renamed from `image_base64`** — Cloudinary URL |
| `body` (attr `message_text`) | `String(2000)` | NOT NULL | Note text (length-capped) |
| `anonymous` (attr `is_anonymous`) | `Boolean` | default `false` | Anonymity flag |
| `status` | `String(24)` | default `"pending_review"`, indexed | Lifecycle: `pending_review` → `approved` / `rejected` |
| `created_at` | `DateTime(tz)` | NOT NULL, indexed | Creation |
| `read_at` | `DateTime(tz)` | NULL | When the recipient opened it |

**Design notes:** Real FKs (CASCADE) to `users.id`. Images **uploaded to Cloudinary, URL stored** (`love_note_service.py:74-99`). Admin review gate with status transitions; sending/receiving gated by user preference flags.

---

### 2.7 `love_note_templates`

**SQLAlchemy class:** `LoveNoteTemplateRecord` · **Pydantic model:** `LoveNoteTemplate` · **Service:** `LoveNoteService` / `AdminLoveNotes`.

| Column | Type | Constraints / Defaults | Purpose |
|---|---|---|---|
| `id` | `UUID` | **PK**, default `_uuid()` | UUID |
| `name` | `String(100)` | **UNIQUE** | Template display name |
| `image_url` | `Text` | NOT NULL | Cloudinary preview URL (**renamed from `preview_image`**) |
| `description` | `String(500)` | NOT NULL | Template description (length-capped) |
| `created_at` | `DateTime(tz)`, default now UTC | NOT NULL | Creation |

**Design notes:** Standalone reference data — no FKs, no relationships. Maintained via admin endpoints.

---

### 2.8 `matches`

**SQLAlchemy class:** `MatchRecord` · **Pydantic model:** `Match` · **Service:** `MatchmakingService`.

| Column | Type | Constraints / Defaults | Purpose |
|---|---|---|---|
| `id` | `UUID` | **PK**, default `_uuid()` | UUID |
| `user_1_regno` | `String(100)` | **FK → `users.regno` ON DELETE CASCADE**, indexed | First participant |
| `user_2_regno` | `String(100)` | **FK → `users.regno` ON DELETE CASCADE**, indexed | Second participant |
| `created_at` | `DateTime(tz)` | NOT NULL, indexed | Match time |
| `expires_at` | `DateTime(tz)` | NOT NULL, indexed | Match window expiry |

**Table-level constraint:** `CheckConstraint("user_1_regno <> user_2_regno", name="ck_matches_distinct_users")` — **a user cannot match with themselves**.

**Design notes:** FKs target `users.regno`. Matches are time-boxed; `conversations.match_id` is UNIQUE against `matches.id` (1:1).

---

### 2.9 `conversations`

**SQLAlchemy class:** `ConversationRecord` · **Pydantic model:** `Conversation`, `ConversationCreate` · **Service:** `ConversationService` / `MessageService`.

Per-match chat channel with an approval state machine (`pending` → `requested` → `accepted` / `rejected`).

| Column | Type | Constraints / Defaults | Purpose |
|---|---|---|---|
| `id` | `UUID` | **PK**, default `_uuid()` | UUID |
| `match_id` (attr `matchId`) | `UUID` | **FK → `matches.id` ON DELETE CASCADE**, **UNIQUE**, indexed | Each match → one conversation |
| `initiator_regno` (attr `initiatorId`) | `String(100)` | **FK → `users.regno` ON DELETE CASCADE**, indexed | Requesting user |
| `receiver_regno` (attr `receiverId`) | `String(100)` | **FK → `users.regno` ON DELETE CASCADE**, indexed | Receiving user |
| `status` | `String(20)` | default `"pending"`, indexed | State machine |
| `requested_at` (attr `requestedAt`) | `DateTime(tz)` | NULL | When the request was sent |
| `accepted_at` (attr `acceptedAt`) | `DateTime(tz)` | NULL | When accepted |
| `created_at` (attr `createdAt`) | `DateTime(tz)` | NOT NULL, indexed | Row creation |
| `last_message_at` | `DateTime(tz)` | NULL | Denormalized latest message time |
| `last_message_text` | `String(160)` | NULL | Denormalized latest message preview |
| `blocked_by` | `ARRAY(String(100))` | default `{}` | Who blocked this conversation (**renamed from JSONB** `blocked_by`) |
| `terminated_at` (attr `terminatedAt`) | `DateTime(tz)` | NULL | End of conversation life |

**Removed column:** `participants` JSONB is **gone** — participants are derived from `initiatorId`/`receiverId`.

**Important current inconsistency:** the service layer (`conversation_service.py:662-671, 719-728`) still writes **`isBlocked` / `blockedBy` / `blockedAt` / `unblockedAt`** via `$set`, and reads `isBlocked` / `blockedBy` keys (`conversation_service.py:428-429, 540-541, 622-623`). The new schema **does not define those columns** (only `blocked_by` ARRAY) — because `_apply()` only sets existing columns, those writes are silently swallowed, and reads fall back to `None`. This is a live bug to reconcile (either map to `blocked_by` in the repo or add the columns). Flagged in section 9.

---

### 2.10 `messages`

**SQLAlchemy class:** `MessageRecord` · **Pydantic model:** `Message` · **Service:** `MessageService`.

| Column | Type | Constraints / Defaults | Purpose |
|---|---|---|---|
| `id` | `UUID` | **PK**, default `_uuid()` | UUID |
| `conversation_id` | `UUID` | **FK → `conversations.id` ON DELETE CASCADE**, indexed | Parent conversation |
| `sender_regno` (attr `sender_id`) | `String(100)` | **FK → `users.regno` ON DELETE CASCADE**, indexed | Sending user |
| `receiver_regno` (attr `receiver_id`) | `String(100)` | **FK → `users.regno` ON DELETE CASCADE**, indexed | Receiving user |
| `body` (attr `text`) | `Text` | NOT NULL | Message body |
| `created_at` (attr `timestamp`) | `DateTime(tz)` | NOT NULL, indexed | Send time |
| `read` | `Boolean` | default `false` | Read receipt |

**Removed columns:** legacy duplicates `matchId`, `senderId`, `receiverId` are **gone** (normalization).

**Table-level constraint:** `Index("ix_messages_conversation_created", "conversation_id", "created_at")` — composite index for thread ordering.

**Design notes:** Services use `sender_id`/`receiver_id` keys, which map to the `sender_regno`/`receiver_regno` columns via `mapped_column("...", ...)` (attribute keys unchanged). True CASCADE parent → child: deleting a conversation removes its messages. Real-time delivery publishes a copy to Redis (`message_service.py:140-151`).

---

### 2.11 `notifications`

**SQLAlchemy class:** `NotificationRecord` · **Pydantic model:** `Notification`, `NotificationContent` · **Service:** `NotificationService`.

| Column | Type | Constraints / Defaults | Purpose |
|---|---|---|---|
| `id` | `UUID` | **PK**, default `_uuid()` | UUID |
| `recipient_regno` (attr `user_id`) | `String(100)` | **FK → `users.regno` ON DELETE CASCADE**, indexed | Recipient |
| `heading` | `String(120)` | NOT NULL | Notification title |
| `body` | `String(500)` | NOT NULL | Notification body |
| `created_at` (attr `timestamp`) | `DateTime(tz)` | NOT NULL, indexed | Creation |
| `read` | `Boolean` | default `false` | Seen flag |

**Normalization:** the JSONB `content` blob is **flattened into `heading` + `body` columns**. Compatibility is preserved in the repository:
- **Write:** `add()` unpacks `content = {heading, body}` into the two columns (`postgres.py:211-214`).
- **Read:** `_dict()` re-bundles `content = {"heading": ..., "body": ...}` (`postgres.py:96-97`).

---

### 2.12 `message_reports`

**SQLAlchemy class:** `MessageReportRecord` · **Pydantic model:** *(none — handled via service dicts)* · **Service:** `MessageService` / `AdminService`.

| Column | Type | Constraints / Defaults | Purpose |
|---|---|---|---|
| `id` | `UUID` | **PK**, default `_uuid()` | UUID |
| `message_id` | `UUID` | **FK → `messages.id` ON DELETE CASCADE**, indexed | Reported message |
| `reporter_regno` (attr `reporter_id`) | `String(100)` | **FK → `users.regno` ON DELETE CASCADE**, indexed | Reporter |
| `reported_user_regno` (attr `reported_user_id`) | `String(100)` | **FK → `users.regno` ON DELETE CASCADE**, indexed | Author of reported message |
| `reason` | `String(1000)` | NOT NULL | Report reason |
| `status` | `String(20)` | default `"pending"`, indexed | `pending` → resolved/dismissed |
| `reported_at` | `DateTime(tz)` | NOT NULL, indexed | Filing time |
| `reviewed_at` | `DateTime(tz)` | NULL | Admin review time |
| `reviewed_by_regno` (attr `reviewed_by`) | `String(100)` | **FK → `users.regno` ON DELETE SET NULL** | Reviewer |

**Constraint:** `UniqueConstraint("message_id", "reporter_regno")` — one report per message per user (enforced at DB level; also checked in service `message_service.py:267-276`).

## 3. Foreign Key Map

### 3.1 Explicit FK constraints (enforced by PostgreSQL)

| Child Table | FK Column (DB) | Parent Table / Column | ON DELETE |
|---|---|---|---|
| `confessions` | `author_id` | `users.id` | **CASCADE** |
| `confession_comments` | `confession_id` | `confessions.id` | **CASCADE** |
| `confession_comments` | `user_id` | `users.id` | **CASCADE** |
| `reports` | `confession_id` | `confessions.id` | **CASCADE** |
| `reports` | `comment_id` | `confession_comments.id` | **CASCADE** |
| `reports` | `reported_by_id` | `users.id` | **CASCADE** |
| `login_tokens` | `user_id` | `users.id` | **CASCADE** |
| `love_notes` | `sender_id` | `users.id` | **CASCADE** |
| `love_notes` | `recipient_id` | `users.id` | **CASCADE** |
| `matches` | `user_1_regno` | `users.regno` | **CASCADE** |
| `matches` | `user_2_regno` | `users.regno` | **CASCADE** |
| `conversations` | `match_id` | `matches.id` | **CASCADE** |
| `conversations` | `initiator_regno` | `users.regno` | **CASCADE** |
| `conversations` | `receiver_regno` | `users.regno` | **CASCADE** |
| `messages` | `conversation_id` | `conversations.id` | **CASCADE** |
| `messages` | `sender_regno` | `users.regno` | **CASCADE** |
| `messages` | `receiver_regno` | `users.regno` | **CASCADE** |
| `notifications` | `recipient_regno` | `users.regno` | **CASCADE** |
| `message_reports` | `message_id` | `messages.id` | **CASCADE** |
| `message_reports` | `reporter_regno` | `users.regno` | **CASCADE** |
| `message_reports` | `reported_user_regno` | `users.regno` | **CASCADE** |
| `message_reports` | `reviewed_by_regno` | `users.regno` | **SET NULL** |

> **Key functional change vs. previous state:** nearly every FK is now **CASCADE** — deleting a user, confession, match, or conversation recursively removes all dependent data at the DB level. Only `message_reports.reviewed_by_regno` uses `SET NULL`.

### 3.2 Check & unique constraints

| Table | Constraint |
|---|---|
| `reports` | `ck_reports_one_target` — exactly one of `confession_id` / `comment_id` is set |
| `reports` | `uq_reports_confession_reporter` — one report per user per confession |
| `reports` | `uq_reports_comment_reporter` — one report per user per comment |
| `matches` | `ck_matches_distinct_users` — `user_1_regno <> user_2_regno` (no self-matches) |
| `message_reports` | `UNIQUE (message_id, reporter_regno)` |
| `conversations` | `UNIQUE (match_id)` — 1:1 conversation per match |

### 3.3 Relationship diagram

```
users (users.id) ──< confessions.author_id                     [CASCADE]
users (users.id) ──< confession_comments.user_id               [CASCADE]
confessions ──< confession_comments.confession_id               [CASCADE]
users (users.id) ──< reports.reported_by_id                     [CASCADE]
confessions ──< reports.confession_id (polymorphic target 1)   [CASCADE]
confession_comments ──< reports.comment_id (polymorphic target 2) [CASCADE]
users (users.id) ──< login_tokens.user_id                       [CASCADE]
users (users.id) ──< love_notes.sender_id / recipient_id        [CASCADE]
users (users.regno) ──< matches.user_1_regno / user_2_regno     [CASCADE]
matches ──< 1:1 > conversations.match_id (UNIQUE)               [CASCADE]
users (users.regno) ──< conversations.initiator_regno / receiver_regno [CASCADE]
conversations ──< messages.conversation_id                      [CASCADE]
users (users.regno) ──< messages.sender_regno / receiver_regno  [CASCADE]
users (users.regno) ──< notifications.recipient_regno           [CASCADE]
messages ──< message_reports.message_id                         [CASCADE]
users (users.regno) ──< message_reports.reporter_regno / reported_user_regno [CASCADE]
users (users.regno) ── mild `message_reports.reviewed_by_regno  [SET NULL]
```

> **Dual-key note (persists):** content/reporting tables (confessions, comments, reports, love_notes, login_tokens) reference **`users.id`**; matchmaking/chat tables (matches, conversations, messages, notifications, message_reports) reference **`users.regno`**. Both are valid FKs, but they split a user's data across two key columns.

---

## 4. Pydantic Data Models

All API-facing models live in `backend/app/models.py`. Key deltas vs. the last commit: `UserInfo.username → name`, `username` dropped from user models, `LoginToken.metadata` → `session_status`/`last_seen_at`/`device`, `LoveNote.image_base64 → image_url`, `LoveNoteTemplate.preview_image → image_url`.

### 4.1 Entity models (persisted, `_id` alias)

| Model | Maps to table | Notable fields |
|---|---|---|
| `UserDetails` | `users` | `Regno`, `Name`, `email`, `which_class`, `gender`, `user_role`, `is_blocked`, preference flags, login metadata |
| `Confession` | `confessions` + `confession_comments` + `reports` | `reactions` dict, reaction counts, `comments`, `reported_by` (derived), `user_reaction` (view field) |
| `ConfessionComment` | `confession_comments` | `user_info` (reconstructed), `likes`/`dislikes`, `reported_by` (derived), `user_reaction` |
| `Report` | `reports` | `content_id`, `content_type` (virtual — mapped to `confession_id`/`comment_id`) |
| `LoginToken` | `login_tokens` | `token_hash`, `session_status`, `last_seen_at`, `device` |
| `LoveNote` | `love_notes` | `image_url` (Cloudinary URL), `status`, `read_at` |
| `LoveNoteTemplate` | `love_note_templates` | `image_url` |
| `Match` | `matches` | `user_1_regno`, `user_2_regno`, `expires_at` |
| `Conversation` | `conversations` | `matchId`, `initiatorId`, `receiverId`, `status` |
| `Message` | `messages` | `matchId`, `senderId`, `receiverId`, `text`, `timestamp` |
| `Notification` | `notifications` | `content` (`NotificationContent`) — flattened to columns by repo |
| `UserInfo` (embedded) | — | `{id, name, avatar}` snapshot |

### 4.2 Input / request models (not persisted)

| Model | Used by | Validation logic |
|---|---|---|
| `ConfessionCreate` | POST confession | `confessing_to` required non-empty after strip |
| `ConfessionUpdate` | PATCH confession | `is_anonymous`, `is_comment` (anonymize-back forbidden) |
| `CommentCreate` | POST comment | `message` required |
| `ReactionCreate` | reaction endpoints | `reaction` string |
| `ReportCreate` | report endpoints | `reason` required |
| `ConversationCreate` | POST conversation request | `matchId` required |
| `AdminUserCreate` / `AdminUserUpdate` | admin user admin | full profile fields, partial updates |
| `LoveNoteCreate` *(defined in `love_note_service.py`)* | POST love note | `recipient_id`, `image_base64`, `message_text`, `is_anonymous` |

### 4.3 Shared/utility models

| Model | Purpose |
|---|---|
| `Token` | JWT response envelope (`access_token`, `token_type`) |
| `NotificationContent` | Structured notification payload (`heading`, `body`) — decomposed by repo |
| `AdminUserBase` | Common base for admin create/update payloads |

---

## 5. Repository & Data Access Layer

**File:** `backend/app/postgres.py`.

### 5.1 `PostgresClient` / `Database`

- Single engine+sessionmaker cached per `DATABASE_URL` (module dicts `_DATABASE_ENGINES` / `_DATABASE_SESSIONS`).
- `Database.__getitem__` / `__getattr__` resolve tables via the `_TABLES` registry → `TableRepository`.
- `clear_all()` deletes rows in dependency-reversed order; `initialize()` runs `create_all`.

### 5.2 `TableRepository` — Mongo-style API over SQLAlchemy

| Method | SQL equivalent | Notes |
|---|---|---|
| `list(query, projection, sort, skip, limit)` | `SELECT ... WHERE ... ORDER BY ...` | Returns `Cursor(list)` (`.sort()`, `.limit()`, `.skip()`); **for comments, batches a user join to reconstruct `user_info`** |
| `get_one(query, projection, sort)` | `SELECT ... LIMIT 1` | First match |
| `add(document)` | `INSERT` | Pops `_id`, **model-aware shims** (comments/reports/notifications), filters to allowed columns |
| `update / update_all / get_and_update` | `UPDATE ...` | `$set` (incl. dotted JSONB paths), `$inc`, `$push`, `$addToSet`, `$pull`, `upsert` |
| `get_and_update` | `SELECT ... FOR UPDATE` then UPDATE | Atomic read-modify-write (token redemption) |
| `delete / delete_all` | `DELETE` | ORM delete |
| `count(query)` | `SELECT COUNT(*)` | Optional filter |
| `distinct(key)` | `SELECT DISTINCT` | Non-null values (used for classes list) |

### 5.3 Compatibility shims between legacy field names and normalized columns

| Target model | Write (`add`) | Read (`_dict`) | Query (`_conditions`) |
|---|---|---|---|
| `confession_comments` | `user_info.id` → `user_id`; drop `user_reaction` | rebuild `user_info` via batched user lookup | `user_info.id` → `user_id` |
| `reports` | `content_id` + `content_type` → `confession_id`/`comment_id` | emit `content_type` + `content_id` | `content_id` → OR over `confession_id`/`comment_id` |
| `notifications` | `content{heading,body}` → `heading`/`body` | re-bundle `content` dict | — |
| `users` / `login_tokens` | — | cast `INET` columns (`last_login_ip`, `request_ip`, `consume_ip`) to `str` | — |

---

## 6. Service Layer Data Handling

| Service | Tables | Read paths | Write paths |
|---|---|---|---|
| `ConfessionService` | confessions, confession_comments, users, reports | List (sort popularity/comments/time), per-user reaction resolution, **reported_by derived from `reports` table** | Create; update settings; react (rewrite `reactions` JSONB + counts); comment (`$inc comment_count`); like/dislike (mutually exclusive); report (insert row, bump report_count) |
| `AuthService` | users, login_tokens | User by Regno; token lookup | Magic-link issue (`create_token.py`), atomic single-use redemption (`FOR UPDATE`), IP bond check, `session_status`/`last_seen_at`/`device` upsert, login metadata flush, JWT mint |
| `LoveNoteService` | love_notes, users, love_note_templates | Template catalog; notes by sender/recipient/status | Create (upload to Cloudinary, store URL, `pending_review`); admin approve/reject (+notifications); delete |
| `MatchmakingService` | users, matches | Eligibility scan (class/gender/opt-in/throttle) | Create match pair, expire |
| `ConversationService` | conversations, matches, users, notifications | Conversation list / status; other-user info | Request/accept/reject (+notifications); block/unblock (**writes `isBlocked`/`blockedBy`/… which are NOT schema columns — see 2.9/9**) |
| `MessageService` | messages, conversations, message_reports, redis | Threads with read receipts | Send (+update `last_message_at`/`last_message_text`; publish to Redis), report message (dedupe) |
| `NotificationService` | notifications, users | Per-user feed (limit 50), mark read/delete | Create (heading/body via `NotificationContent.rsplit`) |
| `ProfileService` | users | Profile read | Update profile, preferences |
| `AdminService` | users, confessions, reports, message_reports, love_notes | Moderation queues/dashboards | Block users, approve/reject content |
| `StorageService` | — (Cloudinary) | Signed URL generation | Upload → URL stored in `profile_picture_url` / `image_url` |
| `RedisService` | Redis | Caching / real-time message fan-out | TTL caching; pub/sub of messages |

**Cross-cutting write patterns:**
- **Derived instead of denormalized** for reports: `reported_by`/`report_count` are computed from the `reports` table on read.
- **Denormalized counters kept** for reactions, comment counts, and last-message preview.
- **Atomic operations** for token redemption (`get_and_update` + `FOR UPDATE`); `$inc` for counters.
- **Repository shims** keep callers on legacy field names while the DB is normalized.

---

## 7. Indexes

| Table | Indexes (beyond PK) |
|---|---|
| `users` | `regno` (UNIQUE), `email` (UNIQUE), `class_name`, `role` |
| `confessions` | `created_at`, `author_id`, `approved` |
| `confession_comments` | `confession_id`, `user_id`, `created_at` |
| `reports` | `confession_id`, `comment_id`, `reported_by_id`, `created_at` |
| `login_tokens` | `token_hash` (UNIQUE), `user_id`, `expires_at`, `session_status` |
| `love_notes` | `sender_id`, `recipient_id`, `status`, `created_at` |
| `love_note_templates` | `name` (UNIQUE) |
| `matches` | `user_1_regno`, `user_2_regno`, `created_at`, `expires_at` |
| `conversations` | `match_id` (UNIQUE), `initiator_regno`, `receiver_regno`, `status`, `created_at` |
| `messages` | `conversation_id`, `sender_regno`, `receiver_regno`, `created_at`, **composite `ix_messages_conversation_created (conversation_id, created_at)`** |
| `notifications` | `recipient_regno`, `created_at` |
| `message_reports` | `message_id`, `reporter_regno`, `reported_user_regno`, `status`, `reported_at` |

---

## 8. Data Flow Summary

```
Client Request
   │
   ▼
Router (backend/app/routers/*) ── auth middleware (get_current_user / admin_required)
   │
   ▼
Pydantic input model ── validation (models.py / service-local models like LoveNoteCreate)
   │
   ▼
Service layer (services/*) ── business rules + derived/denormalized data
   │
   ▼
TableRepository (postgres.py) ── legacy-field names in, normalized columns out (shims)
   │
   ▼
PostgreSQL (UUID PKs, ARRAY, INET, JSONB reactions, real FKs + CASCADE, check/unique constraints)
   │
   ▼
ORM rows mapped through _dict() shims → Pydantic entity models (computed user_info,
   content_id/content_type, user_reaction) → JSON response
```

**Cache / real-time tier:** Redis (`redis://redis:6379`) for TTL caching and message pub/sub.
**Media tier:** user avatars, love-note images, templates stored in Cloudinary; DB stores only URLs/identifiers (`profile_picture_url`, `image_url`).

---

## 9. Data Integrity, Security & Known Issues

### 9.1 Security measures
- **Passwordless auth** — nothing password-like stored. Magic tokens SHA-256 hashed, single-use, 1-hour expiry (see `create_token.py:31`), IP-bound.
- **JWT** signed with `SECRET_KEY` (HS256), 30-min expiry, `sub` = Regno.
- **`is_anonymous` one-way toggle** — public confessions can never be re-anonymized.
- **Row-level lock** (`SELECT ... FOR UPDATE`) for atomic token redemption.
- **Referential integrity now DB-enforced** — nearly all FKs are real and `CASCADE` (no more orphan-prone soft references on user_id/content_id).
- **Constraint-enforced dedupe** — unique report pairs, unique message-report pairs, unique match→conversation, `ck_reports_one_target`, `ck_matches_distinct_users`.
- **Admin gating** on `user_role == "admin"` for all admin/moderation routers.

### 9.2 Integrity concerns (current working tree)
1. **Block/unblock writes are silently lost** — `ConversationService` persists `isBlocked` / `blockedBy` / `blockedAt` / `unblockedAt` and reads `isBlocked`/`blockedBy`, but the schema only has `blocked_by ARRAY`. Repository `_apply()` ignores non-existent columns, so blocks never save and read back as `None`. **Needs either repo shim (map to `blocked_by`) or schema columns.**
2. **Two key columns for users** (`id` vs `regno`) still referenced across different tables.
3. **`confessions.reactions` JSONB** remains a denormalized user-ID array that grows unbounded per confession; counts could drift (no reconciliation job).
4. **Denormalized counters** (`comment_count`, reaction counts, `last_message_*`) are app-maintained; drift under partial failure or direct SQL changes.
5. **No Alembic migrations** — `create_all` creates missing tables but does **not** alter existing tables; applying this working-tree schema to an existing DB requires manual `ALTER TABLE`.
6. **`reports.reported_by_name` / love notes sender/recipient names** are denormalized snapshots; they go stale if a user renames.
7. **Redis and PostgreSQL writes are not transactional** — `send_message` writes the DB row then publishes to Redis; a crash between the two loses the real-time event (message itself survives in PostgreSQL).
8. **Message expiry is app-side only** (4h from `createdAt`), not a DB constraint; messages older than the window are still queryable by admins.

### 9.3 Data lifecycle
- **Created:** via service layer; server-side defaults (UTC timestamps, empty arrays/dicts, zero counters, `false` booleans); `server_default` values at the column level for direct SQL inserts.
- **Read:** repository shims rows back into legacy-shaped dicts; Pydantic re-validates and adds view fields (`user_reaction`, `user_info`, `content_id`/`content_type`).
- **Updated:** `$set`/`$inc`/`$push`/`$pull`; ownership checks (`confession_service.py:168`); one-way anonymity.
- **Deleted:** cascade at the DB level propagates user/confession/conversation/message deletions; soft flags (`approved`, `is_blocked`, `revoked`) still used for moderation.
- **Archived:** message archival handled in `services/message_archival_service.py`.

---

*Report reflects the **current working-tree** state of `backend/app/db_models.py`, `backend/app/models.py`, `backend/app/postgres.py`, and `backend/app/services/*` (schema normalization vs. the last commit is uncommitted).*