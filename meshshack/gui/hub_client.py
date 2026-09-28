"""Talks to the logger's localhost API without blocking the UI thread."""

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Signal


class HubError(Exception):
    pass


class HubClient(QObject):
    # (callback, result, error): emitted from a worker thread, delivered on the UI thread.
    _done = Signal(object, object, object)

    def __init__(self, db_path, parent=None):
        super().__init__(parent)
        self.info_path = db_path.parent / "hub.json"
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hub")
        self._closed = False
        self._done.connect(self._dispatch)

    def available(self):
        return self.info_path.exists()

    def call(self, method, path, body=None, callback=None, timeout=30):
        """callback(result, error) runs on the UI thread; exactly one of them is None."""
        if self._closed:  # a timer can still fire while the window closes
            return
        future = self._pool.submit(self._request, method, path, body, timeout)
        future.add_done_callback(lambda f: self._done.emit(callback, *self._outcome(f)))

    def get(self, path, callback=None):
        self.call("GET", path, callback=callback, timeout=10)

    def post(self, path, body=None, callback=None):
        self.call("POST", path, body or {}, callback=callback)

    @staticmethod
    def _outcome(future):
        try:
            return future.result(), None
        except Exception as ex:
            return None, ex

    def _dispatch(self, callback, result, error):
        if callback:
            callback(result, error)

    def _request(self, method, path, body, timeout):
        # Re-read every time: the logger writes a fresh token each time it starts.
        try:
            info = json.loads(self.info_path.read_text())
        except (OSError, ValueError):
            raise HubError("Logger not running (start `meshshack run`)")
        req = urllib.request.Request(
            info["url"] + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as ex:
            try:
                message = json.loads(ex.read()).get("error", str(ex))
            except ValueError:
                message = str(ex)
            raise HubError(message)
        except (urllib.error.URLError, OSError) as ex:
            raise HubError(f"Logger not reachable: {getattr(ex, 'reason', ex)}")

    def shutdown(self):
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
