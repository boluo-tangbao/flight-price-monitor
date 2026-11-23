"""Isolated adapter for the pinned upstream Tongcheng desktop workflow."""
from pathlib import Path
from urllib.parse import urlsplit
from contextlib import contextmanager

from .tongcheng_upstream.browser import BaseCrawler
from .tongcheng_upstream.roundtrip import InternationalRoundTrip
from .tongcheng_upstream.challenge import ensure_slider_handler


class TongchengDesktop(InternationalRoundTrip, BaseCrawler):
    name = "tongcheng"
    profile_name = "tongcheng-pc"
    use_mobile = False
    AUTH_COOKIES = ("memberIdNew", "cnUser")

    def __init__(self, crawler, diagnostics=None, capture=None):
        config = dict(crawler.config)
        config["user_data_dir"] = str(Path(crawler.user_data_root).resolve())
        channel = config.get("tongcheng_pc_browser_channel")
        if channel:
            config["browser_channel"] = channel
        super().__init__(config, crawler.logger)
        self.diagnostics = diagnostics if diagnostics is not None else {}
        self.capture = capture
        self.last_outcome = None
        self.probe_mode = True
        handler = ensure_slider_handler(config, user_data_dir=self.user_data_root,
                                        profile=self.profile_name, logger=self.logger)
        if handler is None and config.get("slider", {}).get("auto") is not False:
            raise RuntimeError("Tongcheng upstream challenge handler unavailable")
        # Upstream keeps its handler globally. This isolated namespace is only
        # used by Tongcheng; bind the active handler to this run's config/profile.
        if getattr(handler, "name", "") in ("solver-handler", "slider-handler"):
            handler.config = config
            handler.user_data_dir = self.user_data_root
            handler.profile = self.profile_name
            handler.logger = self.logger

    def new_page(self, ctx):
        page = super().new_page(ctx)
        if self.capture is not None:
            page.on("response", self.capture)
        def count_search(request):
            target = urlsplit(request.url)
            if (request.resource_type == "document" and target.hostname == "www.ly.com"
                    and target.path == "/iflight/book1.html"):
                self.diagnostics["search_document_requests"] = self.diagnostics.get("search_document_requests", 0) + 1
        page.on("request", count_search)
        return page

    @contextmanager
    def browser(self, *args, **kwargs):
        with super().browser(*args, **kwargs) as ctx:
            yield ctx
            reader = getattr(self, 'detail_reader', None)
            if reader is not None and self.last_outcome in ('ok', 'error') and self._last_page is not None:
                self.detail_quotes = reader(self._last_page)

    def _watch_retry_after(self, page):
        # The pilot records only safe response counts/statuses via capture.
        # Upstream's scene dumps and scheduler backoff are outside this adapter.
        def page_status(response):
            target = urlsplit(response.url)
            if target.hostname == "www.ly.com" and target.path == "/iflight/book1.html":
                self.diagnostics["page_http_status"] = response.status
        page.on("response", page_status)

    def path_gate(self, path, dim="", **kwargs):
        # Explicit isolated acceptance only; no production policy/scheduler state.
        return True

    def path_note(self, path, status, **kwargs):
        self.last_outcome = status

    def _sleep(self):
        # The pilot has exactly one date; there is no next query to pace.
        pass

    @staticmethod
    def _exc_status(exc):
        from json import JSONDecodeError
        if isinstance(exc, (JSONDecodeError, UnicodeDecodeError)):
            return "parse_error"
        name = type(exc).__name__.lower()
        if "json" in name or "decode" in name:
            return "parse_error"
        if isinstance(exc, TimeoutError) or "timeout" in name:
            return "timeout"
        return "error"
