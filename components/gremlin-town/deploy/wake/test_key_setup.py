import http.client
import socket
import time
import threading
import unittest
from urllib.parse import urlencode
from unittest.mock import patch
import key_setup as setup


class FormTests(unittest.TestCase):
    def setUp(self):
        setup.saved = False
        self.server = setup.HTTPServer(('127.0.0.1', 0), setup.Handler)
        setup.PORT = self.server.server_address[1]
        setup.ORIGIN = f'http://127.0.0.1:{setup.PORT}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.installer = patch.object(setup.subprocess, 'run', side_effect=AssertionError('Installer must not run'))
        self.mock = self.installer.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.installer.stop()
        self.mock.assert_not_called()

    def request(self, method='POST', csrf_value=None, **overrides):
        headers = {'Origin': setup.ORIGIN, 'Cookie': f'gt_setup={setup.cookie}',
                   'Content-Type': 'application/x-www-form-urlencoded'}
        headers.update(overrides)
        payload = urlencode({'csrf': setup.csrf if csrf_value is None else csrf_value, 'url': 'https://example.com/test', 'key': 'invalid'})
        conn = http.client.HTTPConnection('127.0.0.1', setup.PORT, timeout=3)
        conn.request(method, '/' if method == 'GET' else '/save', payload if method == 'POST' else None, headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        conn.close()
        return result

    def test_referrer_policy_preserves_same_origin_form_origin(self):
        self.assertEqual(self.request('GET')[1]['Referrer-Policy'], 'same-origin')

    def test_idle_browser_connection_does_not_block_form(self):
        idle = socket.create_connection(('127.0.0.1', setup.PORT), timeout=1)
        try:
            time.sleep(0.1)
            self.assertEqual(self.request('GET')[0], 200)
        finally:
            idle.close()

    def test_valid_session_reaches_input_validation(self):
        self.assertEqual(self.request()[0], 400)

    def test_unrelated_cookie_does_not_break_session(self):
        self.assertEqual(self.request(Cookie=f'other=test; gt_setup={setup.cookie}')[0], 400)

    def test_wrong_cookie_rejected(self):
        self.assertEqual(self.request(Cookie='gt_setup=wrong')[0], 403)

    def test_missing_cookie_rejected(self):
        self.assertEqual(self.request(Cookie='')[0], 403)

    def test_duplicate_cookie_rejected(self):
        self.assertEqual(self.request(Cookie=f'gt_setup={setup.cookie}; gt_setup={setup.cookie}')[0], 403)

    def test_wrong_csrf_rejected(self):
        with patch.object(setup, 'validate', side_effect=AssertionError('Must reject before validation')) as validator:
            self.assertEqual(self.request(csrf_value='wrong')[0], 400)
            validator.assert_not_called()

    def test_null_origin_rejected(self):
        self.assertEqual(self.request(Origin='null')[0], 403)

    def test_foreign_origin_rejected(self):
        self.assertEqual(self.request(Origin='https://example.com')[0], 403)

    def test_wrong_host_rejected(self):
        self.assertEqual(self.request(Host='example.com')[0], 403)


if __name__ == '__main__':
    unittest.main()
