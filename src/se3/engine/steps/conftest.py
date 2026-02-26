# Prevent pytest from collecting step handler modules as tests.
# The file test.py contains the test_handler function which pytest
# incorrectly treats as a test function.
collect_ignore = ["test.py"]
