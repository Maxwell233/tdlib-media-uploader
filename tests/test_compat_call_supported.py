import unittest
from unittest.mock import patch

from tdlib_media_uploader.core.compat import call_supported


class TestCompatCallSupported(unittest.TestCase):
    def test_zero_args(self):
        self.assertEqual(call_supported(lambda: 'ok'), 'ok')

    def test_unsupported_kwargs(self):
        def collaborator(a):
            return a
        self.assertEqual(call_supported(collaborator, a=1, unused=2), 1)

    def test_varkwargs_and_positional_only(self):
        def arbitrary(**kwargs):
            return kwargs
        def positional(a, /, b):
            return a + b
        self.assertEqual(call_supported(arbitrary, a=1, b=2), {'a': 1, 'b': 2})
        self.assertEqual(call_supported(positional, a=1, b=2), 3)

    def test_inspect_signature_fallbacks(self):
        for error in (TypeError, ValueError):
            calls = []
            def collaborator(*args, **kwargs):
                calls.append((args, kwargs))
                return 'fallback result'
            with self.subTest(error=error.__name__), patch(
                'tdlib_media_uploader.core.compat.inspect.signature',
                side_effect=error('signature unavailable'),
            ):
                result = call_supported(collaborator, (1,), extra=2)
            self.assertEqual(result, 'fallback result')
            self.assertEqual(calls, [((1,), {'extra': 2})])

    def test_collaborator_exception_propagation(self):
        # A collaborator's TypeError must not cause a second invocation.
        for error_type in (TypeError, RuntimeError):
            for inspect_failure in (False, True):
                calls = []
                error = error_type('collaborator failed')
                def collaborator(a):
                    calls.append(a)
                    raise error
                signature = patch(
                    'tdlib_media_uploader.core.compat.inspect.signature',
                    side_effect=ValueError('unavailable'),
                ) if inspect_failure else patch(
                    'tdlib_media_uploader.core.compat.inspect.signature',
                    wraps=__import__('inspect').signature,
                )
                with self.subTest(error=error_type, fallback=inspect_failure), signature:
                    with self.assertRaises(error_type) as raised:
                        call_supported(collaborator, a=1)
                self.assertIs(raised.exception, error)
                self.assertEqual(calls, [1])


if __name__ == '__main__':
    unittest.main()
