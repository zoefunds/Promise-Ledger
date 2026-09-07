import logging


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: live network integration test")
    lg = logging.getLogger("gltest")
    lg.disabled = False
    lg.propagate = True
    lg.setLevel(logging.DEBUG)
