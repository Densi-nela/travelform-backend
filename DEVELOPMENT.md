# Backend layout and code style

The backend uses Django with separate modules for request handling, validation,
notification content, delivery and supporting-document storage.

| Location | Responsibility |
| --- | --- |
| `config/settings.py` | Environment, database, security and service configuration |
| `config/urls.py` | Developer portal and API entry points |
| `travel/models.py` | Database records and constraints |
| `travel/views.py` | Travel workflow endpoints, reporting and attachment access |
| `travel/portal_management.py` | Travel-admin accounts and department management |
| `travel/validation.py` | Submission validation |
| `travel/roles.py` | Shared authorization and administrator selection |
| `travel/admin.py`, `travel/user_forms.py` | Developer administration and role forms |
| `travel/notifications.py` | Durable outbox, delivery claims and retries |
| `travel/email/` | Email rendering, transport adapters and safe error descriptions |
| `travel/supporting_documents.py` | Private S3 upload and download handling |
| `travel/management/commands/` | Notification retry and email preview commands |
| `travel/tests.py` | Workflow and integration regression tests |
| `travel/migrations/` | Versioned database schema history |

## Conventions

Use four-space indentation, grouped imports and a 100-character formatting target.
Let Ruff format multiline calls, collections and expressions. Keep section headings
for distinct responsibilities in larger modules. Comments should explain constraints
or non-obvious decisions; module docstrings should explain the file's purpose.
Preserve settings sections and keep environment-specific values in `.env`.

Ruff is a development dependency; it is not needed by the deployed application.
Historical migrations are excluded from formatting to keep schema history stable.

Run these commands from `backend`:

```powershell
uv sync --locked
uv run --locked ruff format .
uv run --locked ruff check .
uv run --locked python manage.py check
uv run --locked python manage.py makemigrations --check --dry-run
uv run --locked python manage.py test travel
```

For a formatting check without edits, use `uv run --locked ruff format --check .`.
