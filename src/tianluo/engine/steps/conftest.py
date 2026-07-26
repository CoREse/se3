# Prevent pytest from collecting step handler modules as tests.
# These are step *implementation* modules whose names (or whose handler
# functions) start with ``test``, so pytest's default collection would
# incorrectly treat them as test modules/functions:
#   - test.py                 -> defines ``test_handler``
#   - test_with_fail_loop.py  -> defines ``test_handler_with_fail_loop``
collect_ignore = ["test.py", "test_with_fail_loop.py"]
