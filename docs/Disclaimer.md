# ConfessIt Disclaimer

**Last updated: October 26, 2026**

ConfessIt is a social and campus-engagement platform operated by an individual developer. It provides anonymous confessions, comments, matchmaking, time-limited chats, love notes, games, and related features. By using the service, you acknowledge the limitations below.

## 1. User-generated content

Confessions, comments, love notes, profile pictures, chat messages, reports, and other user-submitted material are created or supplied by users. The presence of content on ConfessIt does not mean the operator endorses, verifies, or agrees with it. Users are responsible for what they submit and for respecting other people's rights and privacy.

ConfessIt uses automated moderation, but automated systems can produce false positives and false negatives. Harmful content may not always be detected, and harmless content may be flagged or hidden by mistake. Human reviewers may reinstate or leave reported content hidden. The current product does not provide a built-in appeal feature, although users can contact the operator at [devathmaj@gmail.com](mailto:devathmaj@gmail.com) to raise a concern.

## 2. Anonymity is limited

Confessions are displayed anonymously to ordinary users, but they are not anonymous to the underlying service. The database can associate content with its author, and when a confession is reported, its author identity is revealed on the admin side for review. A user may also reveal their identity through the content they choose to post.

Do not treat a confession, comment, love note, or chat as a guaranteed anonymous communication channel against the operator, administrators, or service infrastructure.

## 3. Chat security and encryption

ConfessIt encrypts stored chat messages using AES-256-GCM, and the browser-to-public-edge connection is protected by TLS in production. However:

- messages are inspected for content safety before encryption;
- ConfessIt is **not end-to-end encrypted** and server-side components can decrypt messages for service processing;
- some internal service-to-service communications are not encrypted by default in the current configuration; and
- no security design can guarantee that data will never be accessed by an unauthorised party or exposed through a compromised account, device, service, or infrastructure.

Do not send passwords, financial data, intimate details, or other highly sensitive information through ConfessIt chats. Encryption is a security measure, not a promise of absolute confidentiality.

## 4. Uploaded images

Profile pictures and love-note images are hosted by Cloudinary. Their URLs may be obscured or signed, but anyone who obtains a usable delivery URL may be able to view the image. Do not upload intimate, confidential, or highly sensitive images, or images you are not authorised to share.

## 5. AI-generated game content

Case Closed and Plot Twist use AI-generated game material such as stories, choices, options, outcomes, and related content. The models may produce incorrect, inconsistent, biased, or inappropriate material. Game output is intended for entertainment and is not professional, educational, legal, medical, or factual advice. Game outcomes are not guaranteed to represent real people or real events.

The prompts used to generate game content are not intended to include players' account details, profile information, or chat messages. Separately, confession text is processed by an embedding model to enable semantic confession search, and submitted content may be processed by moderation services as described in the Privacy Policy.

## 6. Matchmaking and relationships

Matchmaking is configured by an administrator, including the groups or classes that are eligible to match. ConfessIt does not verify that another user's identity, intentions, age, statements, or behaviour are genuine. It does not guarantee successful matches, romantic compatibility, friendship, trustworthiness, or the safety of an interaction. Exercise judgment when communicating with other users and do not share personal information unless you are comfortable doing so.

## 7. Service availability and third parties

ConfessIt relies on third-party services for cloud hosting, email delivery, AI processing, and image hosting. These services may be unavailable, change their functionality, or experience faults outside the operator's control. The operator does not guarantee uninterrupted availability, complete preservation of content, or error-free operation.

## 8. No emergency service

ConfessIt is not an emergency response or crisis service. If you or another person faces an immediate risk, contact the appropriate local emergency service or trusted authority. You may also report content or behaviour through the service where available.

## 9. Limitation of liability

To the maximum extent permitted by applicable law, the operator disclaims warranties not expressly provided and will not be liable for indirect, incidental, special, consequential, or punitive losses arising from use of the service, user content, third-party conduct, matchmaking, AI-generated content, or service outages. Nothing in this Disclaimer excludes rights or liabilities that cannot lawfully be excluded or limited.

This Disclaimer should be read together with the [Privacy Policy](Privacy-Policy.md) and [Terms of Service](Terms-of-Service.md). It is not a substitute for legal advice and does not override mandatory legal protections.

## 10. Contact

Questions or concerns may be sent to [devathmaj@gmail.com](mailto:devathmaj@gmail.com).
