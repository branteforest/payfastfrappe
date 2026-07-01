import frappe
from frappe.tests.utils import FrappeTestCase


class TestPayFastSettings(FrappeTestCase):
    def test_single_exists(self):
        s = frappe.get_single("PayFast Settings")
        self.assertEqual(s.doctype, "PayFast Settings")

    def setUp(self):
        self.settings = frappe.get_single("PayFast Settings")
        self._orig = {
            "environment": self.settings.environment,
            "sandbox_merchant_id": self.settings.sandbox_merchant_id,
            "sandbox_merchant_key": self.settings.sandbox_merchant_key,
            "live_merchant_id": self.settings.live_merchant_id,
            "live_merchant_key": self.settings.live_merchant_key,
            "enabled": self.settings.enabled,
        }

    def tearDown(self):
        for k, v in self._orig.items():
            self.settings.set(k, v)
        try:
            self.settings.save(ignore_permissions=True)
        except frappe.ValidationError:
            self.settings.enabled = 0
            self.settings.save(ignore_permissions=True)

    def test_enabled_requires_active_environment_credentials(self):
        self.settings.environment = "Sandbox"
        self.settings.sandbox_merchant_id = ""
        self.settings.sandbox_merchant_key = ""
        self.settings.enabled = 1
        self.assertRaises(frappe.ValidationError, self.settings.save, ignore_permissions=True)

    def test_disabled_skips_credential_check(self):
        self.settings.environment = "Sandbox"
        self.settings.sandbox_merchant_id = ""
        self.settings.sandbox_merchant_key = ""
        self.settings.enabled = 0
        # Must not raise while disabled -- operators can stage config before flipping the switch.
        self.settings.save(ignore_permissions=True)
        self.assertEqual(self.settings.enabled, 0)

    def test_live_environment_checks_live_credentials_not_sandbox(self):
        self.settings.environment = "Live"
        self.settings.live_merchant_id = ""
        self.settings.live_merchant_key = ""
        self.settings.sandbox_merchant_id = "10000100"
        self.settings.sandbox_merchant_key = "sandboxkey"
        self.settings.enabled = 1
        self.assertRaises(frappe.ValidationError, self.settings.save, ignore_permissions=True)

    def test_complete_credentials_pass_validation(self):
        self.settings.environment = "Sandbox"
        self.settings.sandbox_merchant_id = "10000100"
        self.settings.sandbox_merchant_key = "46f0cd69b5816e2726fbe6b1"
        self.settings.enabled = 1
        self.settings.save(ignore_permissions=True)
        self.assertEqual(self.settings.enabled, 1)
