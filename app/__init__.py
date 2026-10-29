"""AXIOM-FOREX M1: provider-neutral forex safety and domain kernel.

The default runtime is research/paper only. There is deliberately no live
order-routing adapter, credential store, or network broker implementation.
"""

__version__ = "0.1.0-m1"
__trading_modes__ = ("RESEARCH", "PAPER")
