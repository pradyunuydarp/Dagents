"""Provider adapters behind the model inventory's provisioning contract.

Each module here serves one provider and is imported only when a capability
naming it is actually loaded, so `app.ml.inventory` stays free of framework
imports. See `app/ml/inventory.py` for the contract the adapters implement.
"""
