"""Isolated adapter for the pinned upstream Tongcheng desktop workflow."""
from pathlib import Path
from urllib.parse import urlsplit
from contextlib import contextmanager

from playwright.sync_api import Error as BrowserError

from .tongcheng_upstream.browser import BaseCrawler
from .tongcheng_upstream.roundtrip import InternationalRoundTrip
from .tongcheng_upstream.challenge import ensure_slider_handler


class TongchengDesktop(InternationalRoundTrip, BaseCrawler):
    name = "tongcheng"
    profile_name = "tongcheng-pc"
    use_mobile = False
    AUTH_COOKIES = ("memberIdNew", "cnUser")

    def __init__(self, crawler, diagnostics=None):
        config = dict(crawler.config)
        self.cloud_storage_state = getattr(crawler, "cloud_storage_state", None)
        if self.cloud_storage_state is not None:
            config["slider"] = {"auto": False}
        config["user_data_dir"] = str(Path(crawler.user_data_root).resolve())
        channel = config.get("tongcheng_pc_browser_channel")
        if channel:
            config["browser_channel"] = channel
        super().__init__(config, crawler.logger)
        self.diagnostics = diagnostics if diagnostics is not None else {}
        self.last_outcome = None
        self.probe_mode = self.cloud_storage_state is None
        if self.cloud_storage_state is not None:
            return
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
        if self.cloud_storage_state is None:
            page = super().new_page(ctx)
        else:
            page = ctx.new_page()
            page.set_default_timeout(self.timeout_ms)
            page.set_default_navigation_timeout(self.timeout_ms)
            self._last_page = page
            self._watch_retry_after(page)
        def count_search(request):
            target = urlsplit(request.url)
            if (request.resource_type == "document" and target.hostname == "www.ly.com"
                    and target.path == "/iflight/book1.html"):
                self.diagnostics["search_document_requests"] = self.diagnostics.get("search_document_requests", 0) + 1
        page.on("request", count_search)
        return page

    @contextmanager
    def browser(self, *args, **kwargs):
        if self.cloud_storage_state is None:
            desktop = super().browser(*args, **kwargs)
        else:
            from core.cloud_session import cloud_desktop
            desktop = cloud_desktop(self.cloud_storage_state, {"width": 1366, "height": 900})
        with desktop as ctx:
            expected = getattr(self, "expected_search_url", None)
            inspect_cloud = self.cloud_storage_state is not None and expected is not None
            if inspect_cloud:
                # Existence only. It does not prove that the service accepts the session.
                try:
                    self.diagnostics["auth_cookie_in_search_context"] = any(
                        item["name"] in self.AUTH_COOKIES and bool(item.get("value"))
                        for item in ctx.cookies([expected]))
                except BrowserError:
                    self.diagnostics["auth_cookie_in_search_context"] = None
            try:
                yield ctx
                reader = getattr(self, 'detail_reader', None)
                if reader is not None and self.last_outcome in ('ok', 'error') and self._last_page is not None:
                    self.detail_quotes = reader(self._last_page)
            finally:
                if inspect_cloud and self._last_page is not None:
                    from .tongcheng_roundtrip import navigation_summary
                    self.diagnostics["final_destination"] = navigation_summary(self._last_page.url, expected)

    def _watch_retry_after(self, page):
        # The pilot records only safe response counts/statuses via capture.
        # Upstream's scene dumps and scheduler backoff are outside this adapter.
        def page_status(response):
            target = urlsplit(response.url)
            if target.hostname == "www.ly.com" and target.path == "/iflight/book1.html":
                self.diagnostics["page_http_status"] = response.status
            if self.cloud_storage_state is None or response.request.resource_type != "document":
                return
            try:
                is_main = response.request.frame == page.main_frame
            except BrowserError:
                return
            if is_main:
                from .tongcheng_roundtrip import navigation_summary
                trail = self.diagnostics.setdefault("document_trail", [])
                if len(trail) < 12 and (expected := getattr(self, "expected_search_url", None)):
                    trail.append(dict(navigation_summary(response.url, expected), http=response.status))
        page.on("response", page_status)

    def path_gate(self, path, dim="", **kwargs):
        # Explicit isolated acceptance only; no production policy/scheduler state.
        return True

    def path_note(self, path, status, **kwargs):
        self.last_outcome = status
        self.diagnostics["list_outcome"] = status

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
