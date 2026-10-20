# Email delivery pipeline (Azure Functions)

## Prerequisites

- Windows 10/11, macOS 13 or newer, or Linux.
- Python 3.10-3.14 installed and available as `python` on Windows or `python3`
  on macOS and Linux. Recent Linux distributions ship Python 3.10+; install a
  newer Python on older releases.
- An Azure subscription with a Storage account and an Azure Communication
  Services Email resource.
- Permission to create queues and tables in the Storage account used below.

Run the setup steps in order before starting the Function App.

## 1. Install Azure CLI and Azure Functions Core Tools

Choose the section for your operating system.

### Windows

Install both tools with `winget`:

```powershell
winget install --exact --id Microsoft.AzureCLI --source winget
winget install --exact --id Microsoft.Azure.FunctionsCoreTools --source winget
```

Close and reopen PowerShell after the installs.

If either command is not found after reopening the terminal, check that the
installer added it to `PATH`.

### macOS

Install Homebrew first if you do not already have it:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Install the Azure CLI and Azure Functions Core Tools:

```bash
brew update && brew install azure-cli
brew tap azure/functions
brew install azure-functions-core-tools@4
```

If an older Core Tools version is already installed, link the new one:

```bash
brew link --overwrite azure-functions-core-tools@4
```

### Linux

The commands below use `apt` and target Debian-based Linux distributions.

Install the Azure CLI with Microsoft's install script:

```bash
sudo apt-get update
sudo apt-get install -y curl gnupg lsb-release
curl -fsSL 'https://azurecliprod.blob.core.windows.net/$root/deb_install.sh' | sudo bash
```

Install the Azure Functions Core Tools package:

```bash
curl https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > microsoft.gpg
sudo mv microsoft.gpg /etc/apt/trusted.gpg.d/microsoft.gpg
sudo sh -c 'echo "deb [arch=amd64] https://packages.microsoft.com/repos/microsoft-ubuntu-$(lsb_release -cs 2>/dev/null)-prod $(lsb_release -cs 2>/dev/null) main" > /etc/apt/sources.list.d/dotnetdev.list'
sudo apt-get update
sudo apt-get install -y azure-functions-core-tools-4
```

### Verify installation (all systems)

Run this once after installing on any operating system:

```bash
az --version
func --version
```

## 2. Sign in to Azure

Sign in with the Azure CLI:

```bash
az login
az account show
```

`az login` opens a browser. If the browser flow is not available, use:

```bash
az login --use-device-code
```

If the account has more than one subscription, select the one that owns the
Storage and Communication Services resources:

```bash
az account set --subscription "<subscription-id-or-name>"
```

## 3. Create and populate `local.settings.json`

From the repository root, enter the Azure Functions directory and create your
local settings file from the tracked example.

**Windows (PowerShell):**

```powershell
cd azure
Copy-Item .\local.settings.json.example .\local.settings.json
```

**macOS/Linux:**

```bash
cd azure
cp local.settings.json.example local.settings.json
```

Copying keeps the example in the repository. If you prefer to move it instead,
use `Rename-Item .\local.settings.json.example .\local.settings.json` on Windows
or `mv local.settings.json.example local.settings.json` on macOS/Linux.

`azure/local.settings.json` is ignored by git and contains credentials. Never
commit it or paste its contents into an issue, screenshot or log.

Populate it with this shape:

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "<storage-connection-string>",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "EMAIL_QUEUE_CONNECTION_STRING": "<storage-connection-string>",
    "EMAIL_QUEUE_NAME": "email-requests-dev",
    "ACS_EMAIL_CONNECTION_STRING": "<acs-connection-string>",
    "ACS_SENDER_ADDRESS": "DoNotReply@<your-verified-domain>"
  }
}
```

### Required values

| Setting | What it means | Where to find it |
| --- | --- | --- |
| `AzureWebJobsStorage` | Connection string the Functions runtime uses for its own internal state. | Azure Portal: **Storage accounts** > your account > **Security + networking** > **Access keys** > **Connection string**. CLI: `az storage account show-connection-string --name <storage-account> --resource-group <resource-group> --query connectionString --output tsv`. For local Azurite, use `UseDevelopmentStorage=true` instead. |
| `FUNCTIONS_WORKER_RUNTIME` | Tells Core Tools which worker to launch. | Always the literal value `python`; nothing to look up. |
| `EMAIL_QUEUE_CONNECTION_STRING` | Storage account that holds the email request queue and, by default, the suppression table. | Same **Access keys** page and CLI command as `AzureWebJobsStorage`. Reuse the same connection string when the queue lives in the same storage account. For local Azurite, use `UseDevelopmentStorage=true`. |
| `EMAIL_QUEUE_NAME` | Name of the queue that carries email requests. The poison queue is created alongside it as `<name>-poison`. | Choose a name such as `email-requests-dev`; no Azure resource lookup is needed. It must be unique within the storage account. |
| `ACS_EMAIL_CONNECTION_STRING` | Connection string for the Azure Communication Services resource that sends email. | Azure Portal: **Communication Services** > your resource > **Keys** > **Connection string**. Use the primary or secondary connection string. |
| `ACS_SENDER_ADDRESS` | The verified sender address shown to recipients as the From address. | Azure Portal: **Communication Services** > your resource > **Email** > **Domains** > your verified domain > **MailFrom** / sender address. It must be a verified sender, not an arbitrary email address. |

Using `UseDevelopmentStorage=true` for either Storage value requires Azurite
to be installed and running locally. If you do not have Azurite, use the real
Storage account connection string from the portal or CLI instead.

### Optional values

These can be omitted and the defaults will be used:

| Setting | What it means | Where to find or choose it |
| --- | --- | --- |
| `EMAIL_SUPPRESSION_TABLE` | Azure Table Storage table for recipients/domains that permanently failed. Default: `EmailSuppression`. The Function App creates the table if needed. | Choose a table name; no manual creation required. |
| `EMAIL_SUPPRESSION_CONNECTION_STRING` | Storage connection string for the suppression table. Defaults to `EMAIL_QUEUE_CONNECTION_STRING`, then `AzureWebJobsStorage`. | Use the Storage **Access keys** page or CLI command above if the table lives in a different account. |
| `EMAIL_DOMAIN_SUPPRESSION_TTL_SECONDS` | How long a domain-level suppression is trusted before the MX record is checked again. Default: `3600` seconds. | Choose a positive number of seconds; this is an application setting, not an Azure resource value. |

`IsEncrypted` can stay `false` for local development. It is not a secret.

Keep the Azure Functions window open while using the app. The backend only ever
needs `EMAIL_FUNCTION_URL`; it never needs the Storage or ACS credentials.

## 4. Create and activate a virtual environment

From the `azure` directory.

**Windows (PowerShell):**

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, allow it only for the current terminal and try
again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

**macOS/Linux:**

On Debian-based Linux, install the virtual-environment package first if it is
missing:

```bash
sudo apt-get install -y python3 python3-venv python3-pip
```

Then create and activate the environment:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
```

Once the environment is active, install the Function App dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 5. Start the Function App

With the virtual environment active and `local.settings.json` populated, run:

```bash
func start
```

Core Tools will list the HTTP and queue functions and start the local endpoint
at `http://localhost:7071`. Keep that terminal running.

Windows note: Core Tools may resolve its Python worker through the `py` launcher
rather than the active virtual environment. If it uses a different interpreter,
install the requirements with that interpreter (for example,
`py -3.10 -m pip install --user -r requirements.txt`) or point the worker at the
virtual environment before starting the host.

If port `7071` is already in use, start on another port and use that port in the
backend URL:

```bash
func start --port 7072
```

## 6. Point the backend at the local Function App

In the backend's `.env`, set:

```dotenv
EMAIL_FUNCTION_URL=http://localhost:7071/api/email
```

Adjust the port if you used `--port`. This URL is all the backend needs; it must
not receive the Storage or ACS credentials.

## About this Function App

This Function App is the asynchronous delivery path for login magic links,
notification emails and blocked-account notices. The backend still mints login
links and prints them to the console for local debugging; it posts the login
link, the notification title/body/optional target link, or the blocked-account
administrator contact to `POST /api/email` instead of building or sending email
itself.

## Functions

| Function | Trigger | Responsibility |
| --- | --- | --- |
| `enqueue_email` | HTTP `POST /api/email` | Validate the request shape; enqueue JSON; return `202`. Never sends email. |
| `process_email` | Storage Queue trigger | Validate each message; send `type: "login"`, `type: "notification"` or `type: "account_blocked"` through ACS Email. |

The Python queue SDK sends ordinary UTF-8 JSON. `host.json` therefore sets
`messageEncoding: "none"` so the queue trigger reads the body directly instead
of attempting the Azure Functions default base64 decode. Keep the sender and
trigger settings in sync if either side changes.

Three request types are supported:

- `login` — requires `link`, `email`, `name`, `regno`.
- `notification` — requires `title`, `body`, `email`, `name`, `regno`; accepts
  an optional absolute http(s) `link` to the page the notice is about.
- `account_blocked` — requires `email`, `name`, `regno`; accepts an optional
  `admin_email` naming one administrator the recipient can contact to appeal.

Any other type, or a request missing the required fields for its type, is
rejected with HTTP 400 by `enqueue_email` and is dropped as invalid if it
appears in the queue.

The greeting uses the account's full `name`, not the registration number. A
login email states that the link expires in 15 minutes, includes an **Open
Link** button plus the raw URL, and provides a plain-text fallback. A
notification email uses the supplied title as its subject and heading, renders
the supplied body, and, when a `link` is present, includes an **Open in
ConfessIt** button plus the raw URL. An `account_blocked` email explains that
the account cannot currently sign in and, when an `admin_email` is present,
gives that administrator's address as the appeal route. All types provide a
plain-text fallback.

## Who receives a notification email

The backend applies every delivery gate before posting to the Function App. The
Function App only renders and sends the title, body and optional link it is
given (or the blocked-account notice and administrator contact); it has no
access to the application database. Participant notices link to the same
conversation or love note their in-app notification opens. Staff notices link to
the reports review queue, the love-notes review queue, or the specific game
console that needs attention.

These switches gate notification email only. A login magic link is sent for
every real, unblocked account regardless of `notifications_enabled` or any staff
notification preference; signing in must not depend on whether the account has
opted into notices. A blocked account gets the `account_blocked` notice instead
of a magic link, also regardless of notification preferences. The backend picks
one unblocked administrator's email at random for that notice, and the login
response remains uniform so it does not reveal whether the account exists or is
blocked.

- A participant is emailed only when their own `notifications_enabled` column
  is true. There are no participant category preferences.
- An admin or moderator needs both `notifications_enabled` and the matching
  `staff_notification_preferences` switch: `reports_enabled` for `report`,
  `love_notes_enabled` for `love_note`, and `games_enabled` for `game`.
- A moderator is never emailed a `game` notification, even if the stored
  preference row says `games_enabled = true`. The role check runs first.
- The in-app notification feed is not filtered by these delivery switches.

A staff notice intended for both roles is stored once and fanned out to each
eligible staff account. Deduplicated notices are emailed only for the newly
inserted row, not for a retry that finds the existing row.

## Permanent failures and suppression state

The queue trigger separates failures into transient and permanent:

- Transient ACS/network failures are re-raised, so the Functions runtime retries
  the message and eventually moves it to the poison queue.
- A permanent ACS 400 is acknowledged. Retrying the same message cannot make
  it valid.
- Before calling ACS, the trigger checks the recipient domain for an MX record.
  A missing MX record (including a null MX) or a non-existent domain is treated
  as a permanent failure for that message. DNS timeouts and resolver failures
  are treated as transient and allowed through to ACS.
- When ACS identifies the recipient/address/domain as the cause, or the MX check
  fails, a suppression row is written to a persistent table. Later messages for
  that recipient are acknowledged and skipped.
- Domain-wide failures are stored only as a domain row. Domain rows are
  rechecked after `EMAIL_DOMAIN_SUPPRESSION_TTL_SECONDS` (one hour by default).
  If the domain now has MX records, a new message is allowed through; if it is
  still invalid, the row is refreshed. This lets a newly registered domain or a
  newly added MX record recover automatically without retrying the old message.
- Address-level failures are stored only for that exact normalized address and
  do not expire. A different address is checked independently; delete the row
  if the same mailbox is later created or the address is corrected.
- A non-recipient 400 (for example, malformed content) is acknowledged as
  permanent but does not suppress the recipient.

The suppression table defaults to `EmailSuppression`. It uses
`EMAIL_SUPPRESSION_CONNECTION_STRING` when set, then
`EMAIL_QUEUE_CONNECTION_STRING`, then `AzureWebJobsStorage`; the table name can
be overridden with `EMAIL_SUPPRESSION_TABLE`. If Table Storage is unavailable,
the trigger logs the failure and continues with delivery rather than making the
suppression store a new single point of failure.

A new address at a domain that is already suppressed is skipped until that
domain's recheck interval elapses. If the domain is still invalid, the new
address is skipped again; if it has recovered, the message proceeds normally.

ACS accepting a send is not proof of inbox delivery. A mailbox that does not
exist at a valid domain can only be learned from a later bounce; this pipeline
does not consume delivery reports, so those bounces cannot be added to the
suppression table.

## Deployment configuration

For a deployed Function App, configure the same names from the setup table as
Application settings in the Function App, using the Azure Portal or CLI. The
local `local.settings.json` file is only for local development.

The backend needs only `EMAIL_FUNCTION_URL`, which points at the deployed
`/api/email` endpoint in production. It never receives the Storage or ACS
connection strings.

## Delivery semantics

- `host.json` sets `batchSize: 1`, so one message is handled per invocation in
  the local single-instance host. This is not a FIFO guarantee. Azure Storage
  Queue is at-least-once; retries, visibility timeouts, competing consumers and
  scale-out can duplicate or reorder messages.
- A failed ACS send is re-raised. The Functions runtime retries it, and after
  `maxDequeueCount` (5) moves it to the poison queue.
- A permanently invalid message is logged and acknowledged so it is not retried
  forever. It is never reported as a successful delivery.
- A send can succeed while the status poll fails; the retry can then deliver a
  duplicate email. The pipeline does not claim exactly-once delivery.
- Logging records request/message identifiers, email type, attempt counts and
  queue delay. It never logs the magic link, notification title, body or target
  link, recipient address, registration number, connection strings, or full
  queue payloads.
