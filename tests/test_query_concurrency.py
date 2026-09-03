"""Regression: a third online query must wait for the bounded query pool."""
import concurrent.futures
import threading
import time
import unittest

import test_vehicle as fixtures


class QueryConcurrencyTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    tearDown = fixtures.ApiTests.tearDown
    ingest = fixtures.StoreTests.ingest
    request = fixtures.ApiTests.request

    def test_third_query_waits_until_one_of_two_slots_is_free(self):
        point = fixtures.parse(fixtures.LIVE[0])
        query = f'/api/query?device=6094510&start={point["t"]-1}&end={point["t"]+1}'
        entered = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        active = 0

        def slow_query(device, start, end, bins):
            nonlocal active
            with lock:
                active += 1
                if active == 2:
                    entered.set()
            release.wait(timeout=5)
            return {'device_id': device, 'start': start, 'end': end, 'total': 0}

        self.store.query = slow_query
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.request, query, 'reader')
            second = pool.submit(self.request, query, 'reader')
            self.assertTrue(entered.wait(2), 'two slow queries must occupy the pool')
            third = pool.submit(self.request, query, 'reader')
            time.sleep(.05)
            third_done_while_full = third.done()
            release.set()
            third_status, third_body = third.result(timeout=2)
            print('\nthird query while two slots are occupied:', third_status, third_body.decode(), flush=True)
            self.assertEqual(first.result()[0], 200)
            self.assertEqual(second.result()[0], 200)
        self.assertFalse(third_done_while_full,
                         'the third request must wait instead of failing immediately')
        self.assertEqual(third_status, 200,
                         'a transiently full query pool must not surface a permanent UI failure')


if __name__ == '__main__':
    unittest.main()
