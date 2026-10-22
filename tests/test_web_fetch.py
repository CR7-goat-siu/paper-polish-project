import copy
import shutil
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

import utils.web_fetch as web_fetch
from utils.config_loader import load_literature_config


class WebFetchRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = copy.deepcopy(load_literature_config())
        self.config["network"].update(
            {
                "cache_dir": str(self.tmp / "cache"),
                "max_retries": 3,
                "backoff_seconds": 0,
                "min_interval_seconds": 0,
            }
        )
        self.calls: list[str] = []

    def _http_error(self, url, status):
        headers = Message()
        return urllib.error.HTTPError(url, status, "test", headers, None)

    def test_permanent_404_is_not_retried(self):
        def transport(url, params, headers, timeout):
            self.calls.append(url)
            raise self._http_error(url, 404)

        web_fetch.set_transport(transport)
        self.addCleanup(web_fetch.set_transport, None)
        with self.assertRaises(web_fetch.FetchError):
            web_fetch.fetch_text(
                "https://api.example.org/missing",
                config=self.config,
                force_refresh=True,
            )
        self.assertEqual(len(self.calls), 1)

    def test_429_is_retried_and_can_recover(self):
        def transport(url, params, headers, timeout):
            self.calls.append(url)
            if len(self.calls) == 1:
                raise self._http_error(url, 429)
            return '{"ok": true}'

        web_fetch.set_transport(transport)
        self.addCleanup(web_fetch.set_transport, None)
        result = web_fetch.fetch_text(
            "https://api.example.org/retry",
            config=self.config,
            force_refresh=True,
        )
        self.assertEqual(result.body, '{"ok": true}')
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
