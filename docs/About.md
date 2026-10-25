# About ConfessIt

ConfessIt is an anonymous social and campus-engagement platform created to make college life more interactive and exciting. It brings together confessions, matchmaking, private conversations, anonymous love notes, and cooperative mini-games in one place.

The project began with a simple motivation: college communities need more engaging ways to participate in events and connect with one another. ConfessIt explores how a social application can combine student interaction, game mechanics, moderation, and cloud-hosted AI features.

## What you can do with ConfessIt

- **Share confessions:** post anonymous confessions and interact through comments.
- **Discover connections:** participate in administrator-configured matchmaking cohorts and time-limited conversations.
- **Send love notes:** submit anonymous notes that pass through a PII-redaction and moderator-approval workflow.
- **Play together:** enjoy couple-oriented games, including **Case Closed** and **Plot Twist**, alongside other mini-games and leaderboard features.
- **Search by meaning:** use semantic search to discover confessions with related meaning, rather than relying only on exact keyword matches.
- **Support a safer community:** use automated content checks, user reports, and administrator/moderator review workflows to address problematic content.

## Built with cloud and AI services

ConfessIt uses a Python/FastAPI backend, a React frontend, PostgreSQL, Redis, WebSockets, and containerised deployment. Its Azure architecture supports application hosting, data storage, moderation, search, game-content generation, and email delivery.

Notable integrations include:

- **Azure AI Content Safety** for moderation checks on confessions, comments, chats, and uploaded profile images.
- **Azure AI Language** for redacting personally identifiable information from love notes before the redacted version is reviewed by a moderator or administrator.
- **Microsoft Foundry embeddings** for semantic confession search, with vectors stored in Azure Database for PostgreSQL Flexible Server using `pgvector`.
- **Microsoft Foundry and, at times, Groq** for generating game content for Case Closed and Plot Twist. The game-generation prompts are designed not to include player account information or player chat messages.
- **Azure Functions, Azure Storage Queue, and Azure Communication Services Email** for queue-based email delivery.
- **Azure Container Apps** and related networking and HTTPS configuration for hosting the application.
- **Cloudinary** for profile-picture and love-note image hosting.

ConfessIt's privacy and security boundaries are described in the [Privacy Policy](Privacy-Policy.md) and [Disclaimer](Disclaimer.md). For a detailed explanation of the Azure components, see the repository's [Azure documentation](azure.md).

## Important privacy note

“Anonymous” describes how confessions are presented to ordinary users; it does not mean the service cannot associate a confession with its author. Chat is not end-to-end encrypted, and uploaded image URLs may be accessible to anyone who obtains a usable URL. Please review the legal and privacy pages before using the service.

## Project links

- **Source code:** [github.com/Devathmaj/ConfessIt-V2](https://github.com/Devathmaj/ConfessIt-V2)
- **Contact:** [devathmaj@gmail.com](mailto:devathmaj@gmail.com)

ConfessIt currently has no paid features.
