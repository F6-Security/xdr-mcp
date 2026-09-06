# Security Policy

## Reporting a vulnerability

Please report security issues privately, not in a public issue.

- Preferred: use GitHub's [private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guiding-contributors-to-report-security-vulnerabilities/privately-reporting-a-security-vulnerability)
  on this repository (Security → Report a vulnerability).
- Alternatively, email `mxdr@f6.ru`.

Please include the version, the environment variables in use (**never the value
of `XDR_API_KEY`**), and the steps to reproduce. We will acknowledge receipt and
keep you updated on the fix.

## Supported versions

The latest released version is supported. Fixes are not backported.

## Scope

This repository is the MCP server only — the client-side process that talks to
an F6 XDR installation. Vulnerabilities in the XDR platform itself are out of
scope here; report those through F6 support.

## Threat model

Points worth understanding before deploying this server. They are properties of
the design, not defects.

**Everything the read tools return reaches the model provider.** Query results
are placed in the model's context: mail addresses and subjects, file names,
hosts, the audit journal. Give the server an API key with the narrowest rights
that make the task possible, and choose the model provider accordingly.

**Section content is attacker-influenced.** Mail bodies, attachment names and
observed domains are written by whoever sent them. That text arrives in the
model's context alongside legitimate results, so it can carry instructions
aimed at the model. This is the reason the write tool is opt-in.

**`xdr_mark_event` creates a rule, not a single mark.** Its `expression` is a
search expression, so a matcher applies to everything it matches, now and in
future. It is applied asynchronously and this server cannot undo it. It is
hidden unless `XDR_ALLOW_WRITE` is set; keep it unset where reads are enough,
and do not add it to a client's auto-approve list.

**The API key goes wherever `XDR_BASE_URL` points.** The URL is validated at
startup — https only, no query or fragment — so the key cannot travel in the
clear or to a malformed address. It is still sent to whatever host the value
names, so treat that variable as part of the credential: a typo or a lookalike
domain discloses the token on the first tool call.

**Certificate verification cannot be turned off.** There is no `verify=False`
switch by design. For an internal CA, point `XDR_CA_BUNDLE` at a PEM bundle.

## What this server does not do

It makes no network calls other than to `XDR_BASE_URL`, reads no environment
variables beyond the ones documented in the README, sends no telemetry, and
writes no files.
