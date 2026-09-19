# DocketProof

> Compliance. Due Diligence. Audit Trails.

DocketProof is a Django-based business compliance and risk mitigation platform designed to help organizations document due diligence, automate employee onboarding, manage compliance records, and maintain complete audit trails.

The platform was developed from real operational requirements within a multi-site retail business where documentation, accountability, and regulatory compliance are critical.

---

## Why DocketProof?

Many organizations still manage compliance using spreadsheets, email chains, shared drives, and paper forms.

DocketProof centralizes these processes into a single platform that provides:

- Digital employee onboarding
- Compliance tracking
- Electronic document signatures
- Incident reporting
- Training management
- Audit logs
- Automated workflows
- Business risk mitigation

The goal is simple:

> **Create evidence that the right process was followed at the right time by the right people.**

---

# Features

## Employee Onboarding

- Digital onboarding workflow
- Multi-step document sequencing
- Automated document delivery
- Employee onboarding dashboard
- New hire tracking

---

## DocuSign Integration

- Automated envelope creation
- Multiple signer support
- Real-time webhook processing
- Automatic document retrieval
- Signed PDF storage
- Envelope status tracking
- Resend functionality

---

## Compliance Management

Track employee compliance including:

- Work permits
- SIN documentation
- Training completion
- Expiry monitoring
- Outstanding documentation

---

## Incident Reporting

Generate detailed incident reports including:

- Site incidents
- Security incidents
- Environmental incidents
- Health & Safety investigations

Features include:

- PDF generation
- Photo attachments
- Investigation tracking
- Email notifications
- Complete audit history

---

## SMS & Email Notifications

Integrated communication tools including:

- SMS notifications
- Email templates
- Automated reminders
- Delivery tracking
- Click tracking

---

## Audit Trails

Every significant action is logged.

Examples include:

- Documents sent
- Documents signed
- Workflow progression
- Incident updates
- User activity
- Compliance changes

Designed to demonstrate organizational due diligence.

---

# Technology

- Python
- Django
- PostgreSQL
- Bootstrap 5
- Celery
- RabbitMQ
- DocuSign API
- Twilio
- Google Workspace

---

# Installation

Clone the repository

```bash
git clone https://github.com/paulfuther/docketproof.git
cd docketproof
```

Create a virtual environment

```bash
python -m venv venv
```

Activate it

macOS / Linux

```bash
source venv/bin/activate
```

Windows

```cmd
venv\Scripts\activate
```

Install dependencies

```bash
pip install -r requirements.txt
```

---

# Environment Variables

Create a `.env` file.

Example:

```text
SECRET_KEY=

DEBUG=True

DB_NAME=
DB_USER=
DB_PASSWORD=
DB_HOST=
DB_PORT=

DOCUSIGN_ACCOUNT_ID=
DOCUSIGN_CLIENT_ID=
DOCUSIGN_USER_ID=
DOCUSIGN_PRIVATE_KEY=

TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_PHONE_NUMBER=

GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
```

---

# Database

Run migrations

```bash
python manage.py migrate
```

Create an administrator

```bash
python manage.py createsuperuser
```

Run the development server

```bash
python manage.py runserver
```

---

# Architecture

```
DocketProof

 ├── Employee Onboarding
 │
 ├── HR Compliance
 │
 ├── Incident Reporting
 │
 ├── DocuSign Integration
 │
 ├── SMS & Email Services
 │
 ├── Training Records
 │
 └── Audit Logging
```

---

# Roadmap

Future development includes:

- AI-powered compliance assistant
- Mobile application
- Advanced reporting
- Role-based analytics
- Multi-tenant architecture
- REST API
- Power BI integration
- Dashboard builder
- Digital policy acknowledgement
- Automated compliance reminders

---

# Project Philosophy

DocketProof is built around one guiding principle:

> **Good documentation reduces business risk.**

Rather than reacting after an incident occurs, organizations should build systems that document training, onboarding, communication, compliance, and operational processes before problems arise.

Documentation is not simply record keeping.

It is evidence of due diligence.

---

# Contributing

Contributions are welcome.

If you discover a bug or have ideas for improvements, please open an issue before submitting a pull request.

---

# License

MIT License

---

# Disclaimer

This software is provided for educational and business process management purposes.

Organizations remain responsible for complying with all applicable employment, privacy, and regulatory requirements within their jurisdiction.