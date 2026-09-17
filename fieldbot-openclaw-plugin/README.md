# Fieldbot OpenClaw plugin

This plugin exposes the existing Fieldbot Odoo operations as typed OpenClaw
tools. The model never receives raw Odoo credentials. All Odoo calls still pass
through the fixed model/method whitelist in `python/odoo_client.py`, and every
chatter post is forcibly converted to an internal `mail.mt_note`.

Build with `npm install && npm run build`, then install the packed tarball with
`openclaw plugins install npm-pack:./fieldbot-openclaw-plugin-1.0.0.tgz`.

