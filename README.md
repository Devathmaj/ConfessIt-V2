<h1 align="center">ConfessIt</h1>

<p align="center">
  <em>An anonymous social and matchmaking platform built around confessions, private connections, love notes, and interactive games.</em>
</p>

<p align="center">
  <img alt="Anonymous confessions" src="https://img.shields.io/badge/Anonymous%20confessions-E91E63?style=for-the-badge" />
  <img alt="Matchmaking" src="https://img.shields.io/badge/Matchmaking-8E44AD?style=for-the-badge" />
  <img alt="Love notes" src="https://img.shields.io/badge/Love%20notes-FF4D6D?style=for-the-badge" />
  <img alt="Games" src="https://img.shields.io/badge/Games-F39C12?style=for-the-badge" />
  <img alt="Moderation" src="https://img.shields.io/badge/Moderation-2980B9?style=for-the-badge" />
</p>

<p align="center">
  <a href="https://react.dev"><img alt="React" src="https://img.shields.io/badge/React-61DAFB?style=for-the-badge&logo=react&logoColor=000000" /></a>
  <a href="https://fastapi.tiangolo.com"><img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=ffffff" /></a>
  <a href="https://www.postgresql.org"><img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&logo=postgresql&logoColor=ffffff" /></a>
  <a href="https://redis.io"><img alt="Redis" src="https://img.shields.io/badge/Redis-DC382D?style=for-the-badge&logo=redis&logoColor=ffffff" /></a>
  <a href="https://www.docker.com"><img alt="Docker" src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=ffffff" /></a>
  <a href="https://azure.microsoft.com"><img alt="Microsoft Azure" src="https://img.shields.io/badge/Microsoft%20Azure-0078D4?style=for-the-badge&logo=microsoftazure&logoColor=ffffff" /></a>
</p>

---

## Overview

**ConfessIt** is a community-oriented social platform where people can share anonymous confessions, connect through matchmaking, send anonymous love notes, and play games together. It combines social features with moderation and privacy-focused workflows so that anonymous participation is supported by clear review controls.

The project brings together a user-facing application, administrative tools, AI-assisted features, and an Azure-hosted backend.

## Features

### Anonymous confessions

<p>
  <img alt="Anonymous posting" src="https://img.shields.io/badge/Anonymous%20posting-E91E63?style=flat-square" />
  <img alt="Semantic search" src="https://img.shields.io/badge/Semantic%20search-8E44AD?style=flat-square" />
  <img alt="Human review" src="https://img.shields.io/badge/Human%20review-2980B9?style=flat-square" />
</p>

- Publish anonymous confessions and interact with posts through comments.
- Search confessions by meaning using semantic search, rather than depending only on exact keyword matches.
- Use automated content checks and a human review workflow for flagged confessions and comments.

### Matchmaking and private chat

<p>
  <img alt="Private chat" src="https://img.shields.io/badge/Private%20chat-8E44AD?style=flat-square" />
  <img alt="Time-limited conversations" src="https://img.shields.io/badge/Time--limited%20conversations-F39C12?style=flat-square" />
  <img alt="Moderated messages" src="https://img.shields.io/badge/Moderated%20messages-2980B9?style=flat-square" />
</p>

- Matchmaking lets users discover a potential connection.
- When a match is established, users can invite one another to a private conversation.
- Conversations are time-limited. Chat messages pass through content moderation before they are encrypted for the application's chat-storage flow.

### Anonymous love notes

<p>
  <img alt="Approval workflow" src="https://img.shields.io/badge/Approval%20workflow-FF4D6D?style=flat-square" />
  <img alt="PII redaction" src="https://img.shields.io/badge/PII%20redaction-16A085?style=flat-square" />
  <img alt="Anonymous delivery" src="https://img.shields.io/badge/Anonymous%20delivery-E91E63?style=flat-square" />
</p>

- Send anonymous love notes through a mandatory moderator/admin approval workflow.
- Personal information is detected and redacted before the note is presented for review, helping protect the sender's anonymity.

### Mini-games and couple games

<p>
  <img alt="Case Closed" src="https://img.shields.io/badge/Case%20Closed-F39C12?style=flat-square" />
  <img alt="Plot Twist" src="https://img.shields.io/badge/Plot%20Twist-8E44AD?style=flat-square" />
  <img alt="Leaderboards" src="https://img.shields.io/badge/Leaderboards-2980B9?style=flat-square" />
</p>

ConfessIt includes individual and paired games, including **Case Closed** and **Plot Twist**, as well as games such as Wordle-style puzzles, Anagram, Couple Questionnaire, Reaction Time, Connections, and Trivia Drop.

Plot Twist uses an AI-assisted content-generation workflow: story packages are generated and validated before publication, while gameplay uses the published package instead of generating every choice live.

The scoring system supports daily, weekly, monthly, and all-time leaderboards, with separate scoring for individual and couple games.

### Moderation and administration

<p>
  <img alt="Admin tools" src="https://img.shields.io/badge/Admin%20tools-2980B9?style=flat-square" />
  <img alt="Reports" src="https://img.shields.io/badge/Reports-E91E63?style=flat-square" />
  <img alt="Image checks" src="https://img.shields.io/badge/Image%20checks-16A085?style=flat-square" />
</p>

- Dedicated admin and moderator dashboards.
- A confession moderation page for reviewing reported or automatically hidden confessions.
- Flagged confessions, comments, and chat messages are hidden from the app and surfaced as reports for administrator review.
- An administrator can reinstate content when it was flagged by mistake or leave it hidden when it should remain unavailable.
- Uploaded profile images are checked by the image-moderation flow.

## Screenshots

The screenshots below walk through the main user experience and the administrative tools, with a short note on what each screen provides.

### User experience

#### Sign in

Users sign in to reach their personalized dashboard, confessions, matches, notes, and games. Authentication is required for the social and matchmaking features while confessions remain anonymous to the community.

![ConfessIt login page](docs/images/login.png)

#### Dashboard

The dashboard is the signed-in home screen. It surfaces the main areas of the app and gives users a quick way to reach confessions, matchmaking, love notes, and games.

![ConfessIt dashboard](docs/images/dashboard.png)

#### Confessions

The confessions feed is where users publish anonymous posts, read others' confessions, and interact through comments. Semantic search helps surface related confessions even when the wording differs.

![Confessions page](docs/images/confessions.png)

#### Matchmaking

Matchmaking presents users with a potential connection. Once a match is established, the two users can move into a time-limited private conversation that passes through content moderation.

![Matchmaking page](docs/images/matchmaking.png)

#### Love notes

Love notes let users send anonymous messages. Every note goes through a moderator/admin approval step, and detected personal information is redacted before review.

![Love notes page](docs/images/love-notes.png)

#### Games

The games area hosts individual and couple games, including Case Closed, Plot Twist, Wordle-style puzzles, Anagram, Couple Questionnaire, Reaction Time, Connections, and Trivia Drop, with daily, weekly, monthly, and all-time leaderboards.

![Games dashboard](docs/images/games.png)

### Administration and moderation

#### Admin dashboard

The admin dashboard gives administrators an overview of the platform and access to the moderation and review tools they need to keep the community safe.

![Admin dashboard](docs/images/admin-dashboard.png)

#### Confession moderation

The moderation page lists reported or automatically hidden confessions so a moderator or administrator can review the content and either reinstate it or leave it hidden.

![Confession moderation page](docs/images/moderation.png)

## Technology stack

| Area | Technologies / services | Role in ConfessIt |
|---|---|---|
| Frontend | React | User-facing application and administrative interfaces |
| Backend | Python, FastAPI | Application APIs and backend workflows |
| Real-time communication | WebSockets | Live chat communication |
| Database | PostgreSQL | Relational application data and confession embeddings |
| Connection pooling | PgBouncer | Pools PostgreSQL connections |
| Vector search | `pgvector`, Microsoft Foundry `text-embedding-3-small` | Stores and searches confession embeddings semantically |
| Cache / supporting services | Redis | Backend support for application features |
| Containers | Docker, Azure Container Apps | Containerized development and Azure hosting |
| Observability | OpenTelemetry, Prometheus, Grafana | Instrumentation, metrics, and monitoring |
| AI and moderation | Microsoft Foundry, Azure AI Content Safety, Azure AI Language | Game-content generation, content checks, and PII redaction |
| Email delivery | Azure Functions, Azure Storage Queue, Azure Communication Services Email | Asynchronous application email delivery |

## Azure architecture

Azure supports several distinct parts of ConfessIt: application hosting, content moderation, privacy protection, semantic search, AI-generated game content, and asynchronous email delivery.

| Azure component | How ConfessIt uses it |
|---|---|
| **Azure Container Apps** | Hosts the containerized frontend and application services. HTTPS ingress and managed TLS certificates can secure a configured custom domain. |
| **Azure AI Content Safety** | Checks confession text, comments, chat messages, and uploaded profile images. Flagged confessions, comments, and chats enter the hide-and-report workflow. |
| **Microsoft Foundry — `text-embedding-3-small`** | Generates 1,536-dimensional embeddings for confession text. Comments are intentionally excluded from embedding generation. |
| **Azure Database for PostgreSQL Flexible Server** | Stores application data and confession embeddings. `pgvector` supports vector similarity search, and PgBouncer provides connection pooling. |
| **Azure AI Language PII detection** | Detects and redacts personal information from love notes before the redacted version is shown to a moderator/admin. |
| **Microsoft Foundry models** | Generate structured story and game content for Case Closed and Plot Twist, including choices, outcomes, results, and weights. |
| **Azure Functions** | Accepts email requests and processes queued email jobs through separate HTTP-triggered and queue-triggered functions in the same Function App. |
| **Azure Storage Queue** | Buffers email jobs so the main application request does not have to perform the entire delivery operation. |
| **Azure Communication Services Email** | Sends emails from the queue-processing workflow. |
| **Virtual Network (VNet)** | Provides the configured network environment for applicable Azure resources. Private connectivity depends on each service's network configuration; being in the same resource group does not by itself make resources private. |

### High-level architecture

```mermaid
flowchart TB
    User[ConfessIt users] --> App[ConfessIt application on Azure Container Apps]
    App --> Safety[Azure AI Content Safety]
    Safety -->|Flagged content| Review[Hide content and create report]
    Review --> Admin[Admin review: reinstate or keep hidden]

    App -->|Confession text| Embed[Microsoft Foundry embeddings]
    Embed --> DB[Azure Database for PostgreSQL Flexible Server]
    DB --- Vector[pgvector]
    DB --- Pool[PgBouncer]
    App <-->|Semantic search| DB

    App -->|Love note| Language[Azure AI Language PII detection]
    Language -->|Redacted note| Approval[Moderator/admin approval]

    GameGen[Game content generation] --> Foundry[Microsoft Foundry models]
    Foundry --> Packages[Validated game packages]
    Packages --> App

    App -->|Email request| HttpFn[HTTP-triggered Azure Function]
    HttpFn --> Queue[Azure Storage Queue]
    Queue --> Worker[Queue-triggered Azure Function]
    Worker --> ACS[Azure Communication Services Email]

    classDef user fill:#E91E63,stroke:#AD1457,color:#ffffff
    classDef app fill:#6C5CE7,stroke:#4A3FB5,color:#ffffff
    classDef azure fill:#0078D4,stroke:#005A9E,color:#ffffff
    classDef data fill:#4169E1,stroke:#2C4BA8,color:#ffffff
    classDef review fill:#F39C12,stroke:#B9770E,color:#ffffff

    class User user
    class App,GameGen,Packages app
    class Safety,Embed,Language,Foundry,HttpFn,Worker,ACS azure
    class DB,Vector,Pool,Queue data
    class Review,Admin,Approval review
```

### Asynchronous email delivery

Email delivery is separated from the main application's request path:

1. The application sends an email request to an HTTP-triggered Azure Function.
2. The function places the email job in Azure Storage Queue.
3. A queue-triggered function in the same Function App receives the job.
4. The worker calls Azure Communication Services Email to send the message.

This decouples accepting an email request from processing delivery. Queue acceptance alone does not mean that an email has reached the recipient's inbox.

### Moderation, privacy, and networking notes

- **Human review remains part of moderation.** Automated checks can hide content and create a report, but an administrator decides whether flagged confessions, comments, or chats should be reinstated or remain hidden.
- **PII redaction reduces exposure; it does not guarantee anonymity.** Indirect or unusual identifying details may still require human judgement.
- **Chat moderation precedes encryption in the application flow.** This description does not imply end-to-end encryption.
- **Network isolation depends on configuration.** A VNet associated with Container Apps does not automatically place every Azure service on the same private network.

For the detailed service-by-service explanation and official Microsoft documentation, see **[Azure architecture and service details](docs/azure.md)**.

## Project documentation

<p>
  <a href="docs/ARCHITECTURE.md"><img alt="System architecture" src="https://img.shields.io/badge/System%20architecture-6C5CE7?style=for-the-badge" /></a>
  <a href="docs/azure.md"><img alt="Azure architecture" src="https://img.shields.io/badge/Azure%20architecture-0078D4?style=for-the-badge&logo=microsoftazure&logoColor=ffffff" /></a>
</p>

- [System architecture and runtime flows](docs/ARCHITECTURE.md)
- [Azure architecture and service details](docs/azure.md)
