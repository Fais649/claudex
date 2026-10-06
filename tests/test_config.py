import tempfile
import unittest
from pathlib import Path

from claudex import router


class ConfigTests(unittest.TestCase):
    def test_config_loads_named_order_and_models(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "claudex.toml"
            config.write_text("""
[router]
providers = ["first", "second"]
input_protocol = "plain"
state_dir = "./state"
[providers.first]
kind = "claude"
config_dir = "~/.claude-first"
fast_model = "sonnet"
strong_model = "opus"
[providers.second]
kind = "codex"
fast_model = "luna"
strong_model = "sol"
""")
            router.configure(str(config))
            self.assertEqual([provider["name"] for provider in router.PROVIDERS], ["first", "second"])
            self.assertFalse(router.STREAM_INPUT)
            self.assertEqual(router.models(router.PROVIDERS[0]), ("sonnet", "opus"))
            self.assertEqual(router.models(router.PROVIDERS[1]), ("luna", "sol"))
