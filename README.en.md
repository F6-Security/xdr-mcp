<p align="center">
  <img src="https://raw.githubusercontent.com/F6-Security/xdr-mcp/HEAD/docs/logo.png" alt="F6" width="220">
</p>

# F6 XDR MCP Server

<p align="right"><a href="https://github.com/F6-Security/xdr-mcp/blob/HEAD/README.md">Русский</a></p>

[![PyPI](https://img.shields.io/pypi/v/xdr-mcp)](https://pypi.org/project/xdr-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/xdr-mcp)](https://pypi.org/project/xdr-mcp/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/F6-Security/xdr-mcp/blob/HEAD/LICENSE)
[![CI](https://github.com/F6-Security/xdr-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/F6-Security/xdr-mcp/actions/workflows/ci.yml)

An MCP server for the [F6 XDR](https://f6.security) platform. It gives an AI
assistant access to alerts, incidents, mail, files, hosts and the audit journal,
searchable in the platform's own query language.

## Tools

| Tool | Purpose |
| --- | --- |
| `xdr_search` | Search a section; an empty query returns the whole section |
| `xdr_count` | Number of records |
| `xdr_get_mapping` | Fields available for search |
| `xdr_get_filters` | Values available for filters |
| `xdr_mark_event` | Mark an object: resolve an alert, flag a false positive |

Sections: `alerts`, `incidents`, `emails`, `files`, `events`, `connections`,
`assets`, `modules`, `audit`, `applications`.

The first four tools only read. `xdr_mark_event` changes data and is off by
default — see Security below.

## Quick start

You need Python 3.10+ and a personal XDR API token.

```json
{
  "mcpServers": {
    "xdr": {
      "command": "uvx",
      "args": ["xdr-mcp"],
      "env": {
        "XDR_BASE_URL": "https://<your-xdr-host>",
        "XDR_API_KEY": "<token>"
      }
    }
  }
}
```

This block works for Claude Desktop, Cursor and VS Code. For Claude Code:

```sh
claude mcp add xdr \
  -e XDR_BASE_URL=https://<your-xdr-host> -e XDR_API_KEY=<token> \
  -- uvx xdr-mcp
```

Instead of `uvx` you can `pip install xdr-mcp` and run `xdr-mcp`, or use the
container — `docker run -i --rm -e XDR_BASE_URL -e XDR_API_KEY ghcr.io/f6-security/xdr-mcp`
(built for `linux/amd64` and `linux/arm64`).

## Environment variables

| Variable | Purpose |
| --- | --- |
| `XDR_BASE_URL` | Address of your XDR installation |
| `XDR_API_KEY` | Personal API token |
| `XDR_ALLOW_WRITE` | `1` enables `xdr_mark_event`. Off by default |
| `XDR_CA_BUNDLE` | PEM bundle for an installation behind an internal CA |

## Example queries

```
alerts        severity : "critical" AND resolved : "false"
alerts        timestamp >= now-1d AND NOT false_positive : "true"
incidents     closed : "false" AND timestamp >= now-30d
emails        is_blocked : "true" AND timestamp >= now-1d
assets        edr_active : "true"
applications  vendor : "Microsoft Corporation"
audit         success : "false" AND timestamp >= now-7d
```

Conditions combine with `AND`, `OR`, `NOT` and group with parentheses. Time
filtering uses the `timestamp` field, relative (`now-1d`, `now-6h`) or absolute.
`xdr_get_mapping` lists the fields a section accepts.

## Security

Everything the read tools return enters the language model's context: mail
addresses and subjects, file names, hosts, the audit journal. Give the server a
token with the narrowest rights that make the task possible.

`xdr_mark_event` creates a matcher — a rule applied to every object matching the
expression, which this tool cannot undo. That is why it is off by default; enable
`XDR_ALLOW_WRITE` deliberately and keep it out of your MCP client's auto-approve
list.

The threat model and how to report a vulnerability are in
[SECURITY.md](https://github.com/F6-Security/xdr-mcp/blob/HEAD/SECURITY.md).

## Contributing

See [CONTRIBUTING.md](https://github.com/F6-Security/xdr-mcp/blob/HEAD/CONTRIBUTING.md).

## License

[Apache License 2.0](https://github.com/F6-Security/xdr-mcp/blob/HEAD/LICENSE)
