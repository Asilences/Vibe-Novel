"""The single-API settings screen can prepare research without sending it."""
import uuid
import unittest
from unittest.mock import Mock

from tests import test_reference_api as fixtures


@unittest.skipUnless(fixtures.TestClient, 'Optional Studio HTTP dependencies')
class SingleAPIResearchPricingTests(unittest.TestCase):
    setUp = fixtures.ReferenceAPITests.setUp
    send = fixtures.ReferenceAPITests.send
    import_book = fixtures.ReferenceAPITests.import_book

    def settings(self, **changes):
        current = self.client.get('/api/settings').json()
        fields = {'base_url': 'https://example.invalid/v1', 'model': 'offline-model',
                  'allow_loopback': False, **changes}
        response = self.client.put('/api/settings', json={
            'expected_hash': current['hash'], 'api': fields}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('fixture-private-key', response.text)
        return response.json()

    def prepare(self):
        self.provider_factory = Mock(side_effect=AssertionError('No model request is allowed'))
        self.client.app.state.jobs._factory = self.provider_factory
        self.settings(api_key='fixture-private-key')
        book = self.import_book()
        self.prefix = '/' + book['book_id']
        changed = self.send(self.prefix + '/policy', {
            'expected_hash': book['hash'], 'outbound': 'excerpts'}, 'PUT')
        self.assertEqual(changed.status_code, 200, changed.text)
        self.request = {'novel_id': 'author', 'chapter_ids': [],
                        'purpose': '检查选择与后果', 'budget_max_usd': 1.0,
                        'max_output_tokens': 1024}

    def preview(self):
        response = self.send(self.prefix + '/batches/preview', self.request)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def create(self, preview):
        return self.send(self.prefix + '/batches', {
            **self.request, 'expected_preview': preview['hash'],
            'idempotency_key': uuid.uuid4().hex})

    def test_price_fields_unlock_preview_and_save_but_never_start_research(self):
        self.prepare()
        missing = self.send(self.prefix + '/batches/preview', self.request)
        self.assertEqual(missing.status_code, 400, missing.text)
        self.assertIn('价格', missing.text)

        saved = self.settings(price_input_usd=0.3, price_output_usd=1.2)
        self.assertTrue(saved['api']['has_api_key'])
        preview = self.preview()
        self.assertGreater(preview['budget']['estimated_cost_usd'], 0)
        candidate = preview['nodes'][0]['route']['candidates'][0]
        self.assertEqual(candidate['price_input_usd'], 0.3)
        self.assertEqual(candidate['price_output_usd'], 1.2)
        batch = self.create(preview)
        self.assertEqual(batch.status_code, 201, batch.text)
        self.assertEqual(batch.json()['state'], 'prepared')
        self.assertFalse((self.workspace / 'config/models.local.json').exists(),
                         'Entering single-API prices must not migrate the registry')
        self.provider_factory.assert_not_called()
        self.assertEqual(self.project.fingerprint(), self.fingerprint)

    def test_price_change_requires_a_new_preview_before_saving(self):
        self.prepare()
        self.settings(price_input_usd=0.3, price_output_usd=1.2)
        preview = self.preview()
        self.settings(price_input_usd=0.4, price_output_usd=1.6)
        stale = self.create(preview)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertNotEqual(preview['hash'], self.preview()['hash'])
        self.provider_factory.assert_not_called()

    def test_price_change_blocks_starting_an_already_prepared_batch(self):
        self.prepare()
        self.settings(price_input_usd=0.3, price_output_usd=1.2)
        batch = self.create(self.preview())
        self.assertEqual(batch.status_code, 201, batch.text)
        batch_path = self.prefix + '/batches/' + batch.json()['id']
        self.settings(price_input_usd=0.4, price_output_usd=1.6)
        stale = self.send(batch_path + '/start')
        self.assertEqual(stale.status_code, 409, stale.text)
        self.provider_factory.assert_not_called()
        self.assertEqual(self.project.fingerprint(), self.fingerprint)


if __name__ == '__main__':
    unittest.main()
