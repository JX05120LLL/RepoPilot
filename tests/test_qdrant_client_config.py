from __future__ import annotations

import unittest
from unittest.mock import patch

from repopilot_guard.qdrant_bootstrap import _local_qdrant_client


class LocalQdrantClientTests(unittest.TestCase):
    def test_local_client_disables_proxy_environment(self) -> None:
        with patch("repopilot_guard.qdrant_bootstrap.QdrantClient") as client:
            _local_qdrant_client("http://127.0.0.1:6333")

        client.assert_called_once_with(url="http://127.0.0.1:6333", trust_env=False)
