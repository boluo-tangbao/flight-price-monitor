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
separate storage and outcome monitoring. Other new monitoring code is independently implemented.
The local project's existing LICENSE remains in place; the adapted component
continues to carry its upstream Apache-2.0 license.
