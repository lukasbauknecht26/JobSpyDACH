from concurrent.futures import ThreadPoolExecutor
import json
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from jobspy.ziprecruiter.manual_browser import ManualBrowserResolver, ManualResolvedLink

SOURCE = "https://www.ziprecruiter.com/ekn/source-token"
TARGET = "https://www.stepstone.de/stellenangebote--example--123-inline.html"


class ManualBrowserTests(unittest.TestCase):
    def setUp(self):
        self.resolver = ManualBrowserResolver(timeout=3)
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.addCleanup(self.executor.shutdown, wait=True)
        self.addCleanup(self.resolver.close)
        opener = patch(
            "jobspy.ziprecruiter.manual_browser.webbrowser.open_new_tab",
            return_value=True,
        )
        self.open_browser = opener.start()
        self.addCleanup(opener.stop)

    def start(self, url=SOURCE, previous_id=None):
        future = self.executor.submit(self.resolver.resolve, url)
        return future, self.wait_for_job(previous_id)

    def wait_for_job(self, previous_id=None):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = self.resolver._status()
            if (
                self.resolver.form_url
                and state["job_id"]
                and state["job_id"] != previous_id
            ):
                return state
            time.sleep(0.005)
        self.fail("No active manual handoff")

    def request(self, route="status", data=None, headers=None):
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        request = Request(
            self.resolver.form_url + route,
            data=json.dumps(data).encode() if data is not None else None,
            headers=request_headers,
        )
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read()

    def act(self, job_id, action="accept", url=TARGET, headers=None):
        code, body = self.request(
            "action", {"job_id": job_id, "action": action, "url": url}, headers
        )
        return code, json.loads(body)

    def test_valid_url_is_returned_without_html_or_http_status(self):
        future, state = self.start()
        code, body = self.act(state["job_id"], url="  " + TARGET + "  ")
        self.assertEqual(code, 200)
        self.assertEqual(body["phase"], "submitted")
        result = future.result(timeout=1)
        self.assertEqual(result, ManualResolvedLink(TARGET))
        self.assertFalse(hasattr(result, "status_code"))
        self.assertFalse(hasattr(result, "text"))
        self.assertEqual(
            self.open_browser.call_args_list[0].args[0], self.resolver.form_url
        )
        self.assertEqual(self.open_browser.call_args_list[1].args[0], SOURCE)

    def test_direct_detail_never_starts_form_or_browser(self):
        for url in ("https://www.ziprecruiter.de/jobs/123-developer", TARGET):
            with self.subTest(url=url):
                self.assertIsNone(self.resolver.resolve(url))
        self.open_browser.assert_not_called()
        self.assertIsNone(self.resolver._server)

    def test_invalid_targets_keep_current_job_active(self):
        future, state = self.start()
        for url in (
            "javascript:alert(1)",
            SOURCE,
            SOURCE + "#fragment",
            "https://www.alertsclk.com/ekn/still-intermediate",
            "https://bad host/job",
            "https://example.com:bad/job",
            "",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.act(state["job_id"], url=url)[0], 400)
        self.assertFalse(future.done())
        self.assertEqual(self.act(state["job_id"])[0], 200)
        self.assertEqual(future.result(timeout=1).url, TARGET)

    def test_parallel_jobs_reuse_form_and_reject_stale_submission(self):
        first, initial = self.start()
        second = self.executor.submit(self.resolver.resolve, SOURCE + "2")
        self.assertEqual(self.resolver._status()["job_id"], initial["job_id"])
        self.assertEqual(self.act(initial["job_id"], "skip")[0], 200)
        self.assertIsNone(first.result(timeout=1))
        next_job = self.wait_for_job(initial["job_id"])
        self.assertEqual(self.act(initial["job_id"])[0], 409)
        self.assertFalse(second.done())
        self.assertEqual(self.act(next_job["job_id"])[0], 200)
        self.assertEqual(second.result(timeout=1).url, TARGET)
        calls = [call.args[0] for call in self.open_browser.call_args_list]
        self.assertEqual(calls.count(self.resolver.form_url), 1)
        self.assertEqual(calls, [self.resolver.form_url, SOURCE, SOURCE + "2"])

    def test_abort_disables_current_and_queued_jobs(self):
        first, state = self.start()
        second = self.executor.submit(self.resolver.resolve, SOURCE + "2")
        self.assertEqual(self.act(state["job_id"], "abort")[0], 200)
        self.assertIsNone(first.result(timeout=1))
        self.assertIsNone(second.result(timeout=1))
        self.assertIsNone(self.resolver.resolve(SOURCE))
        self.assertEqual(self.open_browser.call_count, 2)

    def test_timeout_disables_fallback_and_rejects_late_input(self):
        self.resolver.timeout = 0.1
        future, state = self.start()
        self.assertIsNone(future.result(timeout=1))
        self.assertEqual(self.resolver._status()["phase"], "expired")
        self.assertEqual(self.act(state["job_id"])[0], 409)
        self.assertIsNone(self.resolver.resolve(SOURCE))
        self.assertEqual(self.open_browser.call_count, 2)

    def test_expired_active_job_cannot_accept_target(self):
        future, state = self.start()
        with self.resolver._state_lock:
            self.resolver._pending.deadline = time.monotonic() - 1
        self.assertEqual(self.act(state["job_id"])[0], 409)
        self.resolver.close()
        self.assertIsNone(future.result(timeout=1))

    def test_close_wakes_waiter_and_stops_server(self):
        future, state = self.start()
        server_thread = self.resolver._server_thread
        self.resolver.close()
        self.assertIsNone(future.result(timeout=1))
        self.assertFalse(server_thread.is_alive())
        self.assertIsNone(self.resolver._server)
        self.assertIsNone(self.resolver.resolve(SOURCE))
        self.resolver.close()

    def test_failed_form_open_disables_fallback(self):
        self.open_browser.return_value = False
        self.assertIsNone(
            self.executor.submit(self.resolver.resolve, SOURCE).result(timeout=1)
        )
        self.assertEqual(self.resolver._status()["phase"], "failed")
        self.assertIsNone(self.resolver.resolve(SOURCE))
        self.assertEqual(self.open_browser.call_count, 1)

    def test_failed_job_open_disables_fallback(self):
        self.open_browser.side_effect = [True, False]
        self.assertIsNone(
            self.executor.submit(self.resolver.resolve, SOURCE).result(timeout=1)
        )
        self.assertIsNone(self.resolver.resolve(SOURCE))
        self.assertEqual(self.open_browser.call_count, 2)

    def test_session_and_origin_are_required(self):
        future, state = self.start()
        self.assertEqual(
            self.act(state["job_id"], headers={"Origin": "https://unrelated.example"})[
                0
            ],
            403,
        )
        self.assertEqual(
            self.act(state["job_id"], headers={"Host": "unrelated.example"})[0], 403
        )
        self.assertEqual(self.request("../wrong-session/status")[0], 404)
        self.assertEqual(self.act("other-job")[0], 409)
        self.assertFalse(future.done())
        origin = "http://" + urlsplit(self.resolver.form_url).netloc
        self.assertEqual(self.act(state["job_id"], headers={"Origin": origin})[0], 200)
        future.result(timeout=1)

    def test_form_serves_static_html_without_interpolating_job_url(self):
        future, state = self.start()
        code, html = self.request("")
        self.assertEqual(code, 200)
        self.assertIn(b"Ziel-URL", html)
        self.assertNotIn(SOURCE.encode(), html)
        self.assertEqual(self.act(state["job_id"], "skip")[0], 200)
        future.result(timeout=1)

    def test_unknown_action_does_not_finish_job(self):
        future, state = self.start()
        self.assertEqual(self.act(state["job_id"], "unknown")[0], 400)
        self.assertFalse(future.done())
        self.assertEqual(self.act(state["job_id"], "skip")[0], 200)
        future.result(timeout=1)


if __name__ == "__main__":
    unittest.main()
