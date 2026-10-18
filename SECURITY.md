# Security

The bridge gives whoever holds `access.token` control of a real desktop session, and on Windows it runs with administrator rights. Treat it accordingly:

- keep the server bound to `127.0.0.1` and reach it through an SSH tunnel;
- store the token like an SSH key (mode 600), never in prompts, logs, screenshots or issues;
- run it only on machines and sessions you are allowed to automate.

The bridge never automates UAC consent or the lock screen.

Please report vulnerabilities privately via GitHub Security Advisories ("Report a vulnerability" on the Security tab) rather than in public issues.
