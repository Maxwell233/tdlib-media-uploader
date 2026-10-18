import unittest
from unittest.mock import patch
from tdlib_media_uploader.core.compat import call_supported

class TestCompatCallSupported(unittest.TestCase):
    def test_zero_args(self):
        def no_args_func():
            return "ok"
        self.assertEqual(call_supported(no_args_func, **{}), "ok")

    def test_unsupported_kwargs(self):
        def simple_func(a):
            return a

        self.assertEqual(call_supported(simple_func, a=1, b=2), 1)

    def test_varkwargs_and_positional_only(self):
        def varkwargs_func(**kwargs):
            return kwargs

        self.assertEqual(call_supported(varkwargs_func, a=1, b=2), {"a": 1, "b": 2})

        # Python syntax for positional-only argument
        def pos_only_func(a, /, b):
            return a + b

        self.assertEqual(call_supported(pos_only_func, a=1, b=2), 3)

    def test_inspect_signature_fallbacks(self):
        def simple_func(a):
            return a

        with patch('inspect.signature', side_effect=TypeError("Mocked TypeError")):
            # It falls back to calling the function blindly with all kwargs if inspect fails
            # But simple_func does not take `b`, so it will fail
            with self.assertRaises(TypeError):
                call_supported(simple_func, a=1, b=2)

        with patch('inspect.signature', side_effect=ValueError("Mocked ValueError")):
            with self.assertRaises(TypeError):
                call_supported(simple_func, a=1, b=2)

    def test_collaborator_exception_propagation(self):
        def failing_func(a):
            raise RuntimeError("Mocked runtime error")

        # Exception should propagate directly
        with self.assertRaises(RuntimeError):
            call_supported(failing_func, a=1)

if __name__ == '__main__':
    unittest.main()
