# Security Policy

Bindery Plugins (the Calibre Bridge plugin and associated tooling) is distributed
alongside [Bindery](https://github.com/vavallee/bindery) and shares its security
posture. The API key set in the plugin config is stored in Calibre's own config
store and is never logged.

With pull mode off (the default) the plugin makes no outbound connections: the
key is only compared against the bearer token Bindery presents when it calls
the plugin.

With pull mode on (0.8.0 and later) the plugin also makes outbound HTTP
requests, to the configured Bindery URL and nowhere else:

- The same key authenticates both directions. The plugin sends it as a bearer
  token on every pull request, so a plugin pointed at a hostile or mistyped
  URL discloses the key to that host. Set the URL with the same care as the
  key.
- HTTPS is always verified against the system trust store plus the optional
  CA file. There is no setting that disables certificate verification.
- Redirects are refused rather than followed, because following one would
  forward the key to the redirect target.
- Plain http is accepted, and the settings dialog warns when the host is not
  loopback, because the key and the books then cross the network unencrypted.
- Downloads are capped (1 GiB per book, 16 MiB per cover), written only under
  a temp directory the plugin creates with a name it chooses, and removed after
  each delivery. A file name or a `coverPath` sent by Bindery is ignored.

## Supported versions

Only the latest release receives security fixes.

| Version | Supported |
| ------- | --------- |
| 0.8.x   | Yes       |
| < 0.8   | No        |

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
