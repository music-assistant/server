# Security Policy

Music Assistant takes the security of our software and services seriously. We appreciate the security research community's efforts in helping us maintain a secure platform for our users.

## Reporting a Vulnerability

If you believe you have found a security vulnerability in Music Assistant, please report it to us through coordinated disclosure.

**Please do not report security vulnerabilities through public GitHub issues, discussions, or pull requests.**

Instead, please report them via GitHub Security Advisories:

1. Navigate to the [Music Assistant Server repository](https://github.com/music-assistant/server)
2. Click on the "Security" tab
3. Click "Report a vulnerability"
4. Fill in the advisory details form

We kindly ask that you allow at least 90 days for us to address the vulnerability before making any public disclosure. This gives us adequate time to develop, test, and release a fix.

### What to Include

Please include as much of the following information as possible to help us better understand and resolve the issue:

- Type of vulnerability (e.g., remote code execution, SQL injection, cross-site scripting, etc.)
- Full paths of source file(s) related to the vulnerability
- The location of the affected source code (tag/branch/commit or direct URL)
- Any special configuration required to reproduce the issue
- Step-by-step instructions to reproduce the issue
- Proof-of-concept or exploit code (if possible)
- Impact of the issue, including how an attacker might exploit it

If you are familiar with the CVSS 3.1 scoring system, please include a vector string using the [official CVSS 3.1 calculator](https://www.first.org/cvss/calculator/3.1).

### Response Timeline

We will make our best effort to respond to your report within 7 days. Please note that Music Assistant, like many open source projects, is relying heavily on volunteers that aren’t full-time resources. We may not be able to respond as quickly as you would like due to other responsibilities.

## Supported Versions

Security updates are only provided for the latest stable release. We strongly encourage all users to keep their Music Assistant installation up to date.

- **Latest stable release**: ✅ Supported
- **Beta/development versions**: ⚠️ Accepted for reporting, but fixes will be released in the next stable version
- **Previous stable releases**: ❌ Not supported
- **Forks or modified versions**: ❌ Not supported

You can find the latest version on our [GitHub releases page](https://github.com/music-assistant/server/releases).

## Scope

### In Scope

Security vulnerabilities in the following areas are in scope:

- Music Assistant Server core application
- Official Music Assistant providers (music providers, player providers, metadata providers, plugins)
- Music Assistant Frontend (web interface)
- Authentication and authorization mechanisms
- API endpoints and data validation
- Configuration handling and storage

### Out of Scope

The following are **not** considered security vulnerabilities:

- **Third-party dependencies**: Vulnerabilities in third-party libraries should be reported to their respective maintainers. We will update dependencies as patches become available.
- **Theoretical vulnerabilities**: Reports must include a working proof of concept or detailed explanation of how the vulnerability can be exploited.
- **Automated scanner results**: Raw output from automated security scanners without validation or proof of exploitability.
- **Social engineering attacks**: Attacks that rely on tricking users into performing actions.
- **Physical access attacks**: Vulnerabilities that require physical access to the device running Music Assistant.
- **Host system compromise**: Vulnerabilities that require prior access to the underlying operating system or container.
- **Malicious music files**: Music Assistant processes audio files provided by users and streaming services. Issues caused by intentionally malicious media files are generally out of scope unless they lead to remote code execution or significant security impact beyond local denial of service.
- **User-installed malicious providers**: Security issues arising from users installing untrusted third-party providers.
- **Privilege escalation for authenticated users**: Music Assistant treats all authenticated users as trusted administrators with full access to the system.
- **Self-inflicted vulnerabilities**: Issues caused by users intentionally misconfiguring their system or disabling security features.

## Public Disclosure & CVE Assignment

We will publish GitHub Security Advisories and through those, will also request CVEs, for valid vulnerabilities that meet the following criteria:

* The vulnerability is in Music Assistant itself, not a third-party library.
* The vulnerability is not already known to us.
* The vulnerability is not already known to the public.
* CVEs will only be requested for vulnerabilities with a severity of medium or higher.

## Recognition

We appreciate the efforts of security researchers who help us keep Music Assistant secure. With your permission, we will publicly acknowledge your responsible disclosure in:

- The security advisory (if published)
- Release notes for the version containing the fix
- Our project documentation

If you prefer to remain anonymous, please let us know in your report.

## Bug Bounty Program

As an open-source project maintained by volunteers, Music Assistant does not offer monetary rewards for vulnerability reports. However, we deeply appreciate your contributions to the security of our project and will recognize your efforts publicly (with your permission).

## Questions

If you have questions about this security policy, please open a discussion in our [GitHub Discussions](https://github.com/music-assistant/server/discussions) or reach out on Discord.
