# Third-party notices

`crawlers/ctrip_roundtrip.py::ProductParser` is adapted from
`dengmeiluan/ticket-monitoring`, `crawlers/ctrip.py`, lines 726–801,
commit `658ae9c9d50f8af656208fd972580752e349d844`.

Source: https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/crawlers/ctrip.py

Upstream is licensed under Apache License 2.0. The full license is retained in
`licenses/ticket-monitoring-Apache-2.0.txt`. Preserve upstream notices when redistributing.
The original repository had no NOTICE file in its root listing at review time.

Changes: extracted parser into a standalone class, required a flight number,
added an independent browser adapter and strict tax-inclusive acceptance,
separate storage and outcome monitoring. Browser components added later are listed below.
The local project's existing LICENSE remains in place; the adapted component
continues to carry its upstream Apache-2.0 license.

`crawlers/ctrip_browser_identity.py` extracts `STEALTH_JS`, its constants, and
`stealth_replacements` from `dengmeiluan/ticket-monitoring/crawlers/base.py`.
`crawlers/ctrip_stealth_ext.py` retains the upstream `crawlers/stealth_ext.py`.
Both use Apache-2.0, commit `5cfa6723f072dffb603724a23b7a2e13cf82c5d1`:

- https://github.com/dengmeiluan/ticket-monitoring/blob/5cfa6723f072dffb603724a23b7a2e13cf82c5d1/crawlers/base.py
- https://github.com/dengmeiluan/ticket-monitoring/blob/5cfa6723f072dffb603724a23b7a2e13cf82c5d1/crawlers/stealth_ext.py

Changes: isolated cloud desktop adapter, derive UA version from the launched browser,
preserve upstream's stable `ctrip` profile seed, combine initialization into one script,
validate complete substitution, and keep local native login behavior unchanged.
The homepage readiness guard independently follows upstream's risk-cookie names and
separate-tab sequence; it stops before search when readiness is missing.
Challenge solvers, profile files, credentials, scheduler, and automatic session
snapshot writeback were not imported.
