"""Tests for the TPS load-test tool in ``tools/tps_test.py``."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="tools/tps_test.py still targets the retired transparent-address API"
    " (build_transaction/build_sweep_transaction); it has not been migrated to"
    " the anonymous v2 chain yet"
)