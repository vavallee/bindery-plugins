# Security Policy

Bindery Plugins (the Calibre Bridge plugin and associated tooling) is distributed
alongside [Bindery](https://github.com/vavallee/bindery) and shares its security
posture. API keys set in the plugin config are stored in Calibre's own config
store and are never logged or transmitted except to your local Bindery instance.

## Supported versions

Only the latest release receives security fixes.

| Version | Supported |
| ------- | --------- |
| 0.2.x   | Yes       |
| < 0.2   | No        |

## Reporting a vulnerability

**Do not open a public issue.** Use one of:

1. **GitHub Security Advisory** (preferred) —
   [github.com/vavallee/bindery-plugins/security/advisories/new](https://github.com/vavallee/bindery-plugins/security/advisories/new).
   This creates a private thread with the maintainers.
2. Email the maintainer listed in the commit metadata.

Please include:

- A description of the issue and its impact.
- Steps to reproduce (PoC welcome).
- The plugin version and Calibre version you tested.

## Disclosure timeline

- **Acknowledgement**: within 7 days.
- **Initial assessment**: within 14 days.
- **Fix target**: 90-day coordinated disclosure window.
- **Credit**: reporters are credited in the release notes by default. Say so if
  you prefer to remain anonymous.

There is no bug bounty.
